# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Implementation of tool support over LSP."""
from __future__ import annotations

import copy
import json
import os
import pathlib
import re
import sys
import sysconfig
import traceback
from typing import Any, Dict, List, Optional, Sequence
from opshin.__main__ import Purpose


# **********************************************************
# Update sys.path before importing any bundled libraries.
# **********************************************************
def update_sys_path(path_to_add: str, strategy: str) -> None:
    """Add given path to `sys.path`."""
    if path_to_add not in sys.path and os.path.isdir(path_to_add):
        if strategy == "useBundled":
            sys.path.insert(0, path_to_add)
        elif strategy == "fromEnvironment":
            sys.path.append(path_to_add)


# Ensure that we can import LSP libraries, and other bundled libraries.
update_sys_path(
    os.fspath(pathlib.Path(__file__).parent.parent / "libs"),
    os.getenv("LS_IMPORT_STRATEGY", "useBundled"),
)

# **********************************************************
# Imports needed for the language server goes below this.
# **********************************************************
# pylint: disable=wrong-import-position,import-error
import lsp_jsonrpc as jsonrpc
import lsp_utils as utils
import lsprotocol.types as lsp
from pygls import server, uris, workspace

WORKSPACE_SETTINGS = {}
GLOBAL_SETTINGS = {}
RUNNER = pathlib.Path(__file__).parent / "lsp_runner.py"

MAX_WORKERS = 5
LSP_SERVER = server.LanguageServer(
    name="OpShin Linter", version="0.1.1", max_workers=MAX_WORKERS
)


# **********************************************************
# Tool specific code goes below this.
# **********************************************************

# Reference:
#  LS Protocol:
#  https://microsoft.github.io/language-server-protocol/specifications/specification-3-16/
#
#  Sample implementations:
#  Pylint: https://github.com/microsoft/vscode-pylint/blob/main/bundled/tool
#  Black: https://github.com/microsoft/vscode-black-formatter/blob/main/bundled/tool
#  isort: https://github.com/microsoft/vscode-isort/blob/main/bundled/tool

TOOL_MODULE = "opshin"

TOOL_DISPLAY = "OpShin Linter"

TOOL_ARGS = [
    "lint",
]  # default arguments always passed to your tool.


# Delete "Linting features" section if your tool is NOT a linter.
# **********************************************************
# Linting features start here
# **********************************************************

#  See `pylint` implementation for a full featured linter extension:
#  Pylint: https://github.com/microsoft/vscode-pylint/blob/main/bundled/tool


@LSP_SERVER.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def did_open(params: lsp.DidOpenTextDocumentParams) -> None:
    """LSP handler for textDocument/didOpen request."""
    document = LSP_SERVER.workspace.get_document(params.text_document.uri)
    diagnostics: list[lsp.Diagnostic] = _linting_helper(document)
    LSP_SERVER.publish_diagnostics(document.uri, diagnostics)


@LSP_SERVER.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def did_save(params: lsp.DidSaveTextDocumentParams) -> None:
    """LSP handler for textDocument/didSave request."""
    document = LSP_SERVER.workspace.get_document(params.text_document.uri)
    diagnostics: list[lsp.Diagnostic] = _linting_helper(document)
    LSP_SERVER.publish_diagnostics(document.uri, diagnostics)


@LSP_SERVER.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def did_close(params: lsp.DidCloseTextDocumentParams) -> None:
    """LSP handler for textDocument/didClose request."""
    document = LSP_SERVER.workspace.get_document(params.text_document.uri)
    # Publishing empty diagnostics to clear the entries for this file.
    LSP_SERVER.publish_diagnostics(document.uri, [])


# output diagnostics when a invalid purpose is given like "#!/usr/bin/env -S opshin eval minntingg"
WRONG_PURPOSE_DIAGNOSTIC = lsp.Diagnostic(
    range=lsp.Range(
        start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=100)
    ),
    message="not a valid script purpose given",
    source=TOOL_DISPLAY,
)


def _linting_helper(document: workspace.Document) -> list[lsp.Diagnostic]:
    # TODO: Determine if your tool supports passing file content via stdin.
    # If you want to support linting on change then your tool will need to
    # support linting over stdin to be effective. Read, and update
    # _run_tool_on_document and _run_tool functions as needed for your project.

    # Determine the purpose of the script

    # default shebang: "#! opshin" -> represents any purpose
    is_default_shebang = re.match(r"^#! *opshin", document.source) is not None

    # check if there is a special shebang e.g., "#!/usr/bin/env -S opshin eval spending"
    shebang = re.match(r"^#!.*opshin.*", document.source)
    if not is_default_shebang and shebang is not None:
        purpose_str = re.sub(r"^#!.*opshin.*eval", "", shebang.group(0)).strip()
        log_always(f"purpose_str: {purpose_str}")
        try:
            purpose = Purpose(purpose_str)
        except ValueError:
            return [WRONG_PURPOSE_DIAGNOSTIC]

    if shebang is None and not is_default_shebang:
        # do not lint at all
        return []

    if is_default_shebang:
        purpose = Purpose.any

    result = _run_tool_on_document(document, extra_args=[f"{purpose.value}"])
    return _parse_output_using_json(result.stdout) if result.stdout else []


def _parse_output_using_json(content: str) -> Sequence[lsp.Diagnostic]:
    """Parse json linter messages and return LSP diagnostic object for each message"""
    diagnostics = []

    messages: List[Dict[str, Any]] = json.loads(content)
    for data in messages:
        start = lsp.Position(
            line=int(data.get("line")),
            character=int(data.get("column")),
        )
        error_class = data.get("error_class")
        message = data.get("message")

        diagnostic = lsp.Diagnostic(
            # TODO: add a better position for the end?
            range=lsp.Range(start=start, end=start),
            message=message,
            code_description=error_class,
            source=TOOL_DISPLAY,
        )
        diagnostics.append(diagnostic)

    return diagnostics


# TODO: if you want to handle setting specific severity for your linter
# in a user configurable way, then look at look at how it is implemented
# for `pylint` extension from our team.
# Pylint: https://github.com/microsoft/vscode-pylint
# Follow the flow of severity from the settings in package.json to the server.
def _get_severity(*_codes: list[str]) -> lsp.DiagnosticSeverity:
    # TODO: All reported issues from linter are treated as warning.
    # change it as appropriate for your linter.
    return lsp.DiagnosticSeverity.Warning


# **********************************************************
# Linting features end here
# **********************************************************


# **********************************************************
# Required Language Server Initialization and Exit handlers.
# **********************************************************
@LSP_SERVER.feature(lsp.INITIALIZE)
def initialize(params: lsp.InitializeParams) -> None:
    """LSP handler for initialize request."""
    log_to_output(f"CWD Server: {os.getcwd()}")

    paths = "\r\n   ".join(sys.path)
    log_to_output(f"sys.path used to run Server:\r\n   {paths}")

    GLOBAL_SETTINGS.update(**params.initialization_options.get("globalSettings", {}))

    settings = params.initialization_options["settings"]
    _update_workspace_settings(settings)
    log_to_output(
        f"Settings used to run Server:\r\n{json.dumps(settings, indent=4, ensure_ascii=False)}\r\n"
    )
    log_to_output(
        f"Global settings:\r\n{json.dumps(GLOBAL_SETTINGS, indent=4, ensure_ascii=False)}\r\n"
    )


@LSP_SERVER.feature(lsp.EXIT)
def on_exit(_params: Optional[Any] = None) -> None:
    """Handle clean up on exit."""
    jsonrpc.shutdown_json_rpc()


@LSP_SERVER.feature(lsp.SHUTDOWN)
def on_shutdown(_params: Optional[Any] = None) -> None:
    """Handle clean up on shutdown."""
    jsonrpc.shutdown_json_rpc()


def _get_global_defaults():
    return {
        "path": GLOBAL_SETTINGS.get("path", []),
        "interpreter": GLOBAL_SETTINGS.get("interpreter", [sys.executable]),
        "args": GLOBAL_SETTINGS.get("args", []),
        "importStrategy": GLOBAL_SETTINGS.get("importStrategy", "useBundled"),
        "showNotifications": GLOBAL_SETTINGS.get("showNotifications", "off"),
    }


def _update_workspace_settings(settings):
    if not settings:
        key = os.getcwd()
        WORKSPACE_SETTINGS[key] = {
            "cwd": key,
            "workspaceFS": key,
            "workspace": uris.from_fs_path(key),
            **_get_global_defaults(),
        }
        return

    for setting in settings:
        key = uris.to_fs_path(setting["workspace"])
        WORKSPACE_SETTINGS[key] = {
            **setting,
            "workspaceFS": key,
        }


def _get_settings_by_path(file_path: pathlib.Path):
    workspaces = {s["workspaceFS"] for s in WORKSPACE_SETTINGS.values()}

    while file_path != file_path.parent:
        str_file_path = str(file_path)
        if str_file_path in workspaces:
            return WORKSPACE_SETTINGS[str_file_path]
        file_path = file_path.parent

    setting_values = list(WORKSPACE_SETTINGS.values())
    return setting_values[0]


def _get_document_key(document: workspace.Document):
    if WORKSPACE_SETTINGS:
        document_workspace = pathlib.Path(document.path)
        workspaces = {s["workspaceFS"] for s in WORKSPACE_SETTINGS.values()}

        # Find workspace settings for the given file.
        while document_workspace != document_workspace.parent:
            if str(document_workspace) in workspaces:
                return str(document_workspace)
            document_workspace = document_workspace.parent

    return None


def _get_settings_by_document(document: workspace.Document | None):
    if document is None or document.path is None:
        return list(WORKSPACE_SETTINGS.values())[0]

    key = _get_document_key(document)
    if key is None:
        # This is either a non-workspace file or there is no workspace.
        key = os.fspath(pathlib.Path(document.path).parent)
        return {
            "cwd": key,
            "workspaceFS": key,
            "workspace": uris.from_fs_path(key),
            **_get_global_defaults(),
        }

    return WORKSPACE_SETTINGS[str(key)]


# *****************************************************
# Internal execution APIs.
# *****************************************************
def _run_tool_on_document(
    document: workspace.Document,
    use_stdin: bool = False,
    extra_args: Sequence[str] = [],
) -> utils.RunResult | None:
    """Runs tool on the given document.

    if use_stdin is true then contents of the document is passed to the
    tool via stdin.
    """
    if str(document.uri).startswith("vscode-notebook-cell"):
        # TODO: Decide on if you want to skip notebook cells.
        # Skip notebook cells
        return None

    if utils.is_stdlib_file(document.path):
        # TODO: Decide on if you want to skip standard library files.
        # Skip standard library python files.
        return None

    # deep copy here to prevent accidentally updating global settings.
    settings = copy.deepcopy(_get_settings_by_document(document))

    code_workspace = settings["workspaceFS"]
    cwd = settings["cwd"]

    use_path = False
    use_rpc = False
    if settings["path"]:
        # 'path' setting takes priority over everything.
        use_path = True
        argv = settings["path"]
    elif settings["interpreter"] and not utils.is_current_interpreter(
        settings["interpreter"][0]
    ):
        # If there is a different interpreter set use JSON-RPC to the subprocess
        # running under that interpreter.
        argv = [TOOL_MODULE]
        use_rpc = True
    else:
        # if the interpreter is same as the interpreter running this
        # process then run as module.
        argv = [TOOL_MODULE]

    argv += TOOL_ARGS + settings["args"] + extra_args

    if use_stdin:
        # TODO: update these to pass the appropriate arguments to provide document contents
        # to tool via stdin.
        # For example, for pylint args for stdin looks like this:
        #     pylint --from-stdin <path>
        # Here `--from-stdin` path is used by pylint to make decisions on the file contents
        # that are being processed. Like, applying exclusion rules.
        # It should look like this when you pass it:
        #     argv += ["--from-stdin", document.path]
        # Read up on how your tool handles contents via stdin. If stdin is not supported use
        # set use_stdin to False, or provide path, what ever is appropriate for your tool.
        argv += []
    else:
        argv += [document.path, "--output-format-json"]

    if use_path:
        # This mode is used when running executables.
        log_to_output(" ".join(argv))
        log_to_output(f"CWD Server: {cwd}")
        result = utils.run_path(
            argv=argv,
            use_stdin=use_stdin,
            cwd=cwd,
            source=document.source.replace("\r\n", "\n"),
        )
        if result.stderr:
            log_to_output(result.stderr)
    elif use_rpc:
        # This mode is used if the interpreter running this server is different from
        # the interpreter used for running this server.
        log_to_output(" ".join(settings["interpreter"] + ["-m"] + argv))
        log_to_output(f"CWD Linter: {cwd}")

        result = jsonrpc.run_over_json_rpc(
            workspace=code_workspace,
            interpreter=settings["interpreter"],
            module=TOOL_MODULE,
            argv=argv,
            use_stdin=use_stdin,
            cwd=cwd,
            source=document.source,
        )
        if result.exception:
            log_error(result.exception)
            result = utils.RunResult(result.stdout, result.stderr)
        elif result.stderr:
            log_to_output(result.stderr)
    else:
        # In this mode the tool is run as a module in the same process as the language server.
        log_to_output(" ".join([sys.executable, "-m"] + argv))
        log_to_output(f"CWD Linter: {cwd}")
        # This is needed to preserve sys.path, in cases where the tool modifies
        # sys.path and that might not work for this scenario next time around.
        with utils.substitute_attr(sys, "path", sys.path[:]):
            try:
                # TODO: `utils.run_module` is equivalent to running `python -m opshin`.
                # If your tool supports a programmatic API then replace the function below
                # with code for your tool. You can also use `utils.run_api` helper, which
                # handles changing working directories, managing io streams, etc.
                # Also update `_run_tool` function and `utils.run_module` in `lsp_runner.py`.
                log_always(f"Running module: {TOOL_MODULE} {argv}")
                result = utils.run_module(
                    module=TOOL_MODULE,
                    argv=argv,
                    use_stdin=use_stdin,
                    cwd=cwd,
                    source=document.source,
                )
            except Exception:
                log_error(traceback.format_exc(chain=True))
                raise
        if result.stderr:
            log_to_output(result.stderr)

    log_to_output(f"{document.uri} :\r\n{result.stdout}")
    return result


def _run_tool(extra_args: Sequence[str]) -> utils.RunResult:
    """Runs tool."""
    # deep copy here to prevent accidentally updating global settings.
    settings = copy.deepcopy(_get_settings_by_document(None))

    code_workspace = settings["workspaceFS"]
    cwd = settings["workspaceFS"]

    use_path = False
    use_rpc = False
    if len(settings["path"]) > 0:
        # 'path' setting takes priority over everything.
        use_path = True
        argv = settings["path"]
    elif len(settings["interpreter"]) > 0 and not utils.is_current_interpreter(
        settings["interpreter"][0]
    ):
        # If there is a different interpreter set use JSON-RPC to the subprocess
        # running under that interpreter.
        argv = [TOOL_MODULE]
        use_rpc = True
    else:
        # if the interpreter is same as the interpreter running this
        # process then run as module.
        argv = [TOOL_MODULE]

    argv += extra_args

    if use_path:
        # This mode is used when running executables.
        log_to_output(" ".join(argv))
        log_to_output(f"CWD Server: {cwd}")
        result = utils.run_path(argv=argv, use_stdin=True, cwd=cwd)
        if result.stderr:
            log_to_output(result.stderr)
    elif use_rpc:
        # This mode is used if the interpreter running this server is different from
        # the interpreter used for running this server.
        log_to_output(" ".join(settings["interpreter"] + ["-m"] + argv))
        log_to_output(f"CWD Linter: {cwd}")
        result = jsonrpc.run_over_json_rpc(
            workspace=code_workspace,
            interpreter=settings["interpreter"],
            module=TOOL_MODULE,
            argv=argv,
            use_stdin=True,
            cwd=cwd,
        )
        if result.exception:
            log_error(result.exception)
            result = utils.RunResult(result.stdout, result.stderr)
        elif result.stderr:
            log_to_output(result.stderr)
    else:
        # In this mode the tool is run as a module in the same process as the language server.
        log_to_output(" ".join([sys.executable, "-m"] + argv))
        log_to_output(f"CWD Linter: {cwd}")
        # This is needed to preserve sys.path, in cases where the tool modifies
        # sys.path and that might not work for this scenario next time around.
        with utils.substitute_attr(sys, "path", sys.path[:]):
            try:
                # TODO: `utils.run_module` is equivalent to running `python -m opshin`.
                # If your tool supports a programmatic API then replace the function below
                # with code for your tool. You can also use `utils.run_api` helper, which
                # handles changing working directories, managing io streams, etc.
                # Also update `_run_tool_on_document` function and `utils.run_module` in `lsp_runner.py`.
                result = utils.run_module(
                    module=TOOL_MODULE, argv=argv, use_stdin=True, cwd=cwd
                )
            except Exception:
                log_error(traceback.format_exc(chain=True))
                raise
        if result.stderr:
            log_to_output(result.stderr)

    log_to_output(f"\r\n{result.stdout}\r\n")
    return result


# *****************************************************
# Logging and notification.
# *****************************************************
def log_to_output(
    message: str, msg_type: lsp.MessageType = lsp.MessageType.Log
) -> None:
    LSP_SERVER.show_message_log(message, msg_type)


def log_error(message: str) -> None:
    LSP_SERVER.show_message_log(message, lsp.MessageType.Error)
    if os.getenv("LS_SHOW_NOTIFICATION", "off") in ["onError", "onWarning", "always"]:
        LSP_SERVER.show_message(message, lsp.MessageType.Error)


def log_warning(message: str) -> None:
    LSP_SERVER.show_message_log(message, lsp.MessageType.Warning)
    if os.getenv("LS_SHOW_NOTIFICATION", "off") in ["onWarning", "always"]:
        LSP_SERVER.show_message(message, lsp.MessageType.Warning)


def log_always(message: str) -> None:
    LSP_SERVER.show_message_log(message, lsp.MessageType.Info)
    if os.getenv("LS_SHOW_NOTIFICATION", "off") in ["always"]:
        LSP_SERVER.show_message(message, lsp.MessageType.Info)


# *****************************************************
# Start the server.
# *****************************************************
if __name__ == "__main__":
    LSP_SERVER.start_io()
