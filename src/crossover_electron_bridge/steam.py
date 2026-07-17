"""Read and validate Steam app manifest (ACF) files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError, OperationalError

_REQUIRED_FIELDS = ("appid", "name", "installdir", "buildid")


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    offset: int


class _SyntaxError(ValueError):
    """Internal marker for malformed Valve ACF syntax."""


def _tokens(source: str) -> list[_Token]:
    """Lex quoted ACF strings and braces, rejecting unquoted syntax."""

    result: list[_Token] = []
    length = len(source)
    index = 0
    while index < length:
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if character in "{}":
            result.append(_Token(character, character, index))
            index += 1
            continue
        if character != '"':
            raise _SyntaxError(f"expected quoted token at offset {index}")

        start = index
        index += 1
        value: list[str] = []
        while index < length:
            character = source[index]
            if character == '"':
                index += 1
                result.append(_Token("string", "".join(value), start))
                break
            if character in "\r\n":
                raise _SyntaxError(f"unterminated quoted token at offset {start}")
            if character == "\\":
                index += 1
                if index >= length:
                    raise _SyntaxError(f"unterminated escape at offset {start}")
                # ACF uses backslash escaping for quotes and backslashes.  Keep
                # unknown escaped characters lossless rather than normalising
                # values that Steam may legitimately have written.
                escaped = source[index]
                if escaped in ('"', "\\"):
                    value.append(escaped)
                else:
                    value.extend(("\\", escaped))
                index += 1
                continue
            value.append(character)
            index += 1
        else:
            raise _SyntaxError(f"unterminated quoted token at offset {start}")
    return result


def _object(tokens: list[_Token], index: int) -> tuple[dict[str, str | dict[str, object]], int]:
    """Parse one brace-delimited object and reject duplicate definitions."""

    fields: dict[str, str | dict[str, object]] = {}
    while index < len(tokens) and tokens[index].kind != "}":
        key = tokens[index]
        if key.kind != "string":
            raise _SyntaxError(f"expected quoted key at offset {key.offset}")
        index += 1
        if key.value in fields:
            raise _SyntaxError(f"duplicate definition for {key.value!r}")
        if index >= len(tokens):
            raise _SyntaxError(f"missing value for {key.value!r}")
        value = tokens[index]
        if value.kind == "{":
            index += 1
            nested, index = _object(tokens, index)
            fields[key.value] = nested
            continue
        if value.kind != "string":
            raise _SyntaxError(f"invalid value for {key.value!r}")
        fields[key.value] = value.value
        index += 1

    if index >= len(tokens) or tokens[index].kind != "}":
        raise _SyntaxError("unterminated object")
    return fields, index + 1


def _parse(source: str) -> dict[str, str | dict[str, object]]:
    tokens = _tokens(source)
    if not tokens:
        raise _SyntaxError("empty manifest")
    if tokens[0].kind != "string" or tokens[0].value != "AppState":
        raise _SyntaxError("manifest must start with an AppState object")
    if len(tokens) < 2 or tokens[1].kind != "{":
        raise _SyntaxError("AppState must be an object")
    fields, index = _object(tokens, 2)
    if index != len(tokens):
        token = tokens[index]
        raise _SyntaxError(f"unexpected token at offset {token.offset}")
    return fields


def read_steam_manifest(path: Path) -> dict[str, str]:
    """Read the four required top-level values from a Steam ACF manifest.

    File access and decoding failures are operational failures.  Syntax and
    manifest-content failures are configuration failures.  Nested objects are
    parsed and validated for structural correctness, but only required fields
    directly under ``AppState`` are returned; nested lookalikes cannot satisfy
    missing top-level fields.
    """

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise OperationalError(f"cannot read Steam app manifest: {path}") from exc

    try:
        fields = _parse(source)
    except _SyntaxError as exc:
        raise ConfigurationError(f"malformed Steam app manifest {path}: {exc}") from exc

    missing = [field for field in _REQUIRED_FIELDS if field not in fields]
    if missing:
        names = ", ".join(missing)
        raise ConfigurationError(f"Steam app manifest missing required field(s): {names}")

    result: dict[str, str] = {}
    for field in _REQUIRED_FIELDS:
        value = fields[field]
        if not isinstance(value, str):
            raise ConfigurationError(
                f"Steam app manifest required field is not a value: {field}"
            )
        if not value:
            raise ConfigurationError(
                f"Steam app manifest required field is empty: {field}"
            )
        result[field] = value
    return result


__all__ = ["read_steam_manifest"]
