"""Versioned, opt-in CLI results. Legacy invocations remain Click-native."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager, redirect_stdout
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

import click
import yaml

from feast.errors import (
    FeastObjectNotFoundException,
    FeastProviderLoginError,
    PermissionNotFoundException,
    ProjectNotFoundException,
)
from feast.operation_report import RepositoryConfigurationMissing
from feast.repo_config import FeastConfigError

MAX_RESULT_BYTES = 1_000_000


@dataclass
class Invocation:
    output: str
    command: str = "feast"
    data: Any = None
    operation_started: bool = False
    completed_projects: list[dict[str, Any]] = field(default_factory=list)
    help_text: Optional[str] = None
    operation_data: Optional[dict[str, Any]] = None


invocation: ContextVar[Optional[Invocation]] = ContextVar(
    "cli_invocation", default=None
)


class StructuredError(click.ClickException):
    """An intentionally public, credential-free error message."""

    def __init__(self, code: str, message: str, hint: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.result_exit_code = exit_code


def machine_output() -> bool:
    return invocation.get() is not None


@contextmanager
def progress_events(path: Path) -> Iterator[Callable[[dict[str, Any]], None]]:
    """Create a separate, exclusive JSONL file accessible only to its owner."""
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
    except FileExistsError as e:
        raise click.UsageError("Progress file already exists; choose a new path") from e
    with os.fdopen(fd, "w", encoding="utf-8") as stream:

        def write(event: dict[str, Any]) -> None:
            stream.write(
                json.dumps({"schema_version": "1", **event}, allow_nan=False) + "\n"
            )
            stream.flush()

        yield write


@contextmanager
def contain_stdout() -> Iterator[None]:
    """Discard incidental output at the CLI process boundary, not in SDK code.

    Results come from explicit return values/reports, never captured text. Cover
    Python prints and native/subprocess fd writes. Like Click's runner, this is
    not a thread-safe embedding API; use separate processes for CLI invocations.
    Third-party code must not retain output handles or outlive the invocation.
    """
    saved_fd = None
    with open(os.devnull, "w") as sink:
        try:
            sys.stdout.flush()
            # Click's in-process test runner has no stdout file descriptor.
            try:
                fd = sys.stdout.fileno()
            except (AttributeError, OSError, ValueError):
                fd = None
            if fd == 1:
                saved_fd = os.dup(1)
                os.dup2(sink.fileno(), 1)
            with redirect_stdout(sink):
                yield
        finally:
            if saved_fd is not None:
                os.dup2(saved_fd, 1)
                os.close(saved_fd)


def _requested_output(args: Sequence[str]) -> Optional[str]:
    """Inspect root options only, including when Click cannot finish parsing.

    Do not mistake dbt's output filename or a positional value for this option.
    """
    result = None
    index = 0
    value_options = {"--chdir", "-c", "--feature-store-yaml", "-f", "--log-level"}
    while index < len(args):
        arg = args[index]
        if arg == "--" or not arg.startswith("-"):
            break
        if arg == "--output":
            index += 1
            if index < len(args):
                result = args[index].lower()
        elif arg.startswith("--output="):
            result = arg.partition("=")[2].lower()
        elif arg in value_options:
            index += 1
        index += 1
    return result if result in ("json", "yaml") else None


def render(value: dict[str, Any], output: str) -> None:
    if output == "json":
        click.echo(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        click.echo(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), nl=False)


def error_response(
    error: BaseException, state: Invocation
) -> tuple[dict[str, Any], int]:
    """Never serialize arbitrary exception text (it may contain credentials)."""
    code, message, hint, exit_code = (
        "OPERATION_FAILED",
        "The operation failed.",
        "Check configuration and provider access before retrying.",
        1,
    )
    if isinstance(error, StructuredError):
        code, message, hint, exit_code = (
            error.code,
            error.message,
            error.hint,
            error.result_exit_code,
        )
    elif isinstance(error, click.UsageError):
        code, message, hint, exit_code = (
            "INVALID_ARGUMENT",
            "Invalid command arguments.",
            "Use the command's --help to check required arguments and options.",
            2,
        )
    elif isinstance(error, FeastProviderLoginError):
        code, message, hint = (
            "AUTHENTICATION_FAILED",
            "Provider authentication failed.",
            "Configure provider credentials and verify access.",
        )
    elif isinstance(error, PermissionError):
        code, message, hint = (
            "PERMISSION_DENIED",
            "Access was denied.",
            "Verify the caller's permissions.",
        )
    elif isinstance(
        error,
        (
            FeastObjectNotFoundException,
            ProjectNotFoundException,
            PermissionNotFoundException,
        ),
    ):
        code, message, hint = (
            "NOT_FOUND",
            "The requested object was not found.",
            "List objects in the configured project and verify the name.",
        )
    elif isinstance(
        error, (RepositoryConfigurationMissing, FeastConfigError, yaml.YAMLError)
    ):
        code, message, hint = (
            "CONFIGURATION_ERROR",
            "The feature repository configuration is missing or invalid.",
            "Use --chdir or --feature-store-yaml to select a valid initialized repository.",
        )
    elif isinstance(error, (click.Abort, KeyboardInterrupt)):
        code, message, hint = (
            "CANCELLED",
            "The operation was cancelled.",
            "Inspect the current state before retrying a mutation.",
        )
    data = None
    if state.operation_started:
        data = state.operation_data or {
            "completed_projects": state.completed_projects,
            "remaining_outcome": "unknown",
        }
    return {
        "schema_version": "1",
        "command": state.command,
        "status": "error",
        "data": data,
        "error": {
            "code": code,
            "message": message,
            "hint": hint,
            "retry_safe": False if state.operation_started else None,
        },
    }, exit_code


class StructuredGroup(click.Group):
    """Catch parsing and execution errors outside Click's standalone handler."""

    def get_help(self, ctx: click.Context) -> str:
        text = super().get_help(ctx)
        state = invocation.get()
        if state is not None:
            state.help_text = text
        return text

    def main(
        self,
        args: Optional[Sequence[str]] = None,
        prog_name: Optional[str] = None,
        complete_var: Optional[str] = None,
        standalone_mode: bool = True,
        **extra: Any,
    ) -> Any:
        arguments = list(sys.argv[1:] if args is None else args)
        output = _requested_output(arguments)
        if output is None:
            return super().main(
                args=arguments,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                **extra,
            )
        state = Invocation(output)
        token = invocation.set(state)
        exit_code = 0
        try:
            try:
                with contain_stdout():
                    result = super().main(
                        args=arguments,
                        prog_name=prog_name,
                        complete_var=complete_var,
                        standalone_mode=False,
                        **extra,
                    )
                if state.help_text is not None and state.data is None:
                    click.echo(state.help_text)
                    return None
                if isinstance(result, int) or state.data is None:
                    raise click.ClickException("Command exited without a result")
                response = {
                    "schema_version": "1",
                    "command": state.command,
                    "status": "success",
                    "data": state.data,
                    "error": None,
                }
                # Check serialization before writing any bytes to stdout.
                encoded = json.dumps(response, allow_nan=False, ensure_ascii=False)
                if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
                    raise StructuredError(
                        "RESULT_TOO_LARGE",
                        "The CLI result exceeds the 1 MB output limit.",
                        "Use narrower filters or an SDK export rather than passing bulk data to an agent.",
                    )
            except (Exception, SystemExit, KeyboardInterrupt) as error:
                if isinstance(error, click.UsageError) and error.ctx is not None:
                    state.command = "feast " + " ".join(
                        error.ctx.command_path.split()[1:]
                    )
                    state.command = state.command.strip()
                response, exit_code = error_response(error, state)
                # If an operation already changed state, its completed-project
                # details may themselves exceed the result limit. Retain the
                # uncertainty and count without rendering a huge error payload.
                if (
                    len(
                        json.dumps(
                            response, allow_nan=False, ensure_ascii=False
                        ).encode("utf-8")
                    )
                    > MAX_RESULT_BYTES
                ):
                    response["data"] = {
                        "completed_count": len(state.completed_projects),
                        "remaining_outcome": "unknown",
                    }
            render(response, output)
        finally:
            invocation.reset(token)
        if standalone_mode or exit_code:
            raise SystemExit(exit_code)
        return state.data
