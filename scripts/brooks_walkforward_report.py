#!/usr/bin/env python3
"""Render a regenerable Portuguese report for a historical Brooks walk-forward.

The run directory is append-only evidence. This script reads its JSON/JSONL
records and writes only ``REPORT.md`` and ``metrics.json`` in that directory.
It never calls an LLM or modifies the recorded run inputs.

Expected input layout (all files are optional while a run is in progress)::

    <root>/run_manifest.json
    <root>/cycles.jsonl                 # one snapshot per expected H1 round
    <root>/role_runs/*.json             # one full record per role invocation
    <root>/frozen_packets/<round_id>.json  # optional full packet sidecars
    <root>/simulation/{trades,fills,equity}.jsonl

The renderer accepts embedded frozen packets and common field aliases as well.
See ``--help`` for the compact record contract. Full model responses stay in
the linked role-run JSON files; the Markdown report only summarizes them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote
from zoneinfo import ZoneInfo


DEFAULT_ROOT = Path("docs/brooks_walkforward_2026-09-20_2026-09-29")
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


def _get(obj: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(obj, Mapping):
        return default
    for key in keys:
        if key in obj and obj[key] is not None:
            return obj[key]
    return default


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str, allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fmt_number(value: Any, digits: int = 4) -> str:
    number = _number(value)
    if number is None:
        return "—"
    return f"{number:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_pct(value: Any, digits: int = 2) -> str:
    number = _number(value)
    return "—" if number is None else f"{_fmt_number(number * 100, digits)}%"


def _fmt_time(value: Any) -> str:
    if value is None:
        return "—"
    dt: datetime | None = None
    number = _number(value)
    if number is not None:
        # The event contract uses epoch milliseconds; tolerate epoch seconds.
        seconds = number / 1000 if abs(number) >= 100_000_000_000 else number
        try:
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return str(value)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return value
    if dt is None:
        return str(value)
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _time_ms(value: Any) -> int | None:
    number = _number(value)
    if number is not None:
        return int(number * 1000 if abs(number) < 100_000_000_000 else number)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


def _markdown_cell(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def _load_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "arquivo ausente"
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"falha de leitura: {exc}"


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str], bool]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.exists():
        return rows, ["arquivo ausente"], False
    try:
        raw = path.read_bytes()
        complete_tail = not raw or raw.endswith(b"\n")
        for line_no, raw_line in enumerate(raw.splitlines(), 1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                errors.append(f"linha {line_no}: {exc}")
                continue
            if isinstance(value, dict):
                rows.append(value)
            else:
                errors.append(f"linha {line_no}: registro precisa ser objeto JSON")
        if not complete_tail:
            errors.append("última linha sem newline; pode ser um append ainda em andamento")
        return rows, errors, True
    except OSError as exc:
        return rows, [f"falha de leitura: {exc}"], True


def _resolve(root: Path, path_value: Any) -> Path | None:
    if not isinstance(path_value, (str, os.PathLike)) or not str(path_value):
        return None
    path = Path(path_value).expanduser()
    candidates = [path] if path.is_absolute() else [root / path, Path.cwd() / path]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].resolve()


def _relative_or_name(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.name


def _link(path: Path, root: Path, label: str | None = None) -> str:
    text = label or _relative_or_name(path, root)
    try:
        rel = os.path.relpath(path.resolve(), root.resolve()).replace(os.sep, "/")
    except (ValueError, OSError):
        return _markdown_cell(text)
    return f"[{_markdown_cell(text)}]({quote(rel, safe='/._-')})"


def _ref_data(ref: Any) -> tuple[Any, str | None, str | None]:
    """Return embedded payload, display path, and expected SHA from a ref."""
    if isinstance(ref, Mapping):
        return (
            _get(ref, "data", "content", "bars", "payload"),
            _get(ref, "path", "file", "filename", "artifact"),
            _get(ref, "sha256", "hash", "content_sha256"),
        )
    if isinstance(ref, str):
        return None, ref, None
    return ref, None, None


def _input_refs(record: Mapping[str, Any], packet: Mapping[str, Any]) -> Mapping[str, Any]:
    refs: dict[str, Any] = {}
    for source in (record, packet):
        for key in ("frozen_files", "input_files", "raw_files", "source_files", "artifacts"):
            value = source.get(key)
            if isinstance(value, Mapping):
                refs.update(value)
    return refs


def _source_summary(
    label: str, payload: Any, ref: Any, root: Path, *, count_bars: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    embedded, path_value, expected_hash = _ref_data(ref)
    if payload is None and embedded is not None:
        payload = embedded
    path = _resolve(root, path_value)
    actual_hash: str | None = None
    digest_kind: str | None = None
    exists = False
    if path is not None and path.is_file():
        try:
            actual_hash = _sha256_bytes(path.read_bytes())
            exists = True
            digest_kind = "arquivo"
        except OSError:
            pass
    if actual_hash is None and expected_hash:
        actual_hash = str(expected_hash)
        digest_kind = "hash informado"
    if actual_hash is None and payload is not None:
        try:
            actual_hash = _sha256_bytes(_canonical_bytes(payload))
            digest_kind = "payload canônico"
        except (TypeError, ValueError):
            actual_hash = None

    if path_value:
        filename = _relative_or_name(path, root) if path else Path(str(path_value)).name
        if path and exists:
            display = _link(path, root, filename)
        else:
            display = _markdown_cell(filename)
    else:
        filename = f"frozen_packet.{label} [inline]" if payload is not None else "sem referência"
        display = _markdown_cell(filename)
    bar_count = len(payload) if count_bars and isinstance(payload, list) else None
    mismatch = bool(expected_hash and actual_hash and str(expected_hash).lower() != actual_hash.lower())
    hash_display = f"sha256:{actual_hash[:12]}…" if actual_hash else "hash indisponível"
    if digest_kind == "payload canônico":
        hash_display += " (derivado)"
    if mismatch:
        hash_display += " (diverge do informado)"
    count_display = f"; {bar_count} barras" if bar_count is not None else ""
    summary = f"{display} — {hash_display}{count_display}"
    detail = {
        "filename": filename,
        "path": _relative_or_name(path, root) if path else (str(path_value) if path_value else None),
        "sha256": actual_hash,
        "sha256_basis": digest_kind,
        "expected_sha256": expected_hash,
        "hash_mismatch": mismatch,
        "bar_count": bar_count,
    }
    return summary, detail


def _find_packet(record: Mapping[str, Any], root: Path, round_id: str | None) -> tuple[dict[str, Any], Path | None]:
    cycle = record.get("cycle") if isinstance(record.get("cycle"), Mapping) else {}
    embedded = _get(cycle, "frozen_packet") or record.get("frozen_packet")
    if isinstance(embedded, Mapping):
        return dict(embedded), None
    candidates = []
    explicit_file = _get(record, "frozen_packet_file", "packet_file")
    if explicit_file:
        explicit_path = _resolve(root, explicit_file)
        if explicit_path:
            candidates.append(explicit_path)
    if round_id:
        candidates.append(root / "frozen_packets" / f"{round_id}.json")
    cycle_id = _get(cycle, "cycle_id")
    if cycle_id:
        candidates.append(root / "frozen_packets" / f"{cycle_id}.json")
    for path in candidates:
        value, error = _load_json(path)
        if error is None and isinstance(value, Mapping):
            packet = _get(value, "frozen_packet", "packet", default=value)
            if isinstance(packet, Mapping):
                return dict(packet), path
    return {}, None


def _canonical_timeframe(value: Any) -> str | None:
    if value is None:
        return None
    key = str(value).strip().upper()
    return {
        "D1": "D1", "1D": "D1", "1DAYS": "D1", "DAY": "D1",
        "H4": "H4", "4H": "H4", "H1": "H1", "1H": "H1",
        "M15": "M15", "15M": "M15", "15MIN": "M15", "M1": "M1", "1M": "M1",
    }.get(key)


def _mapping_alias(mapping: Mapping[str, Any], timeframe: str) -> Any:
    aliases = {
        "D1": ("D1", "1d", "1D", "day"),
        "H4": ("H4", "4h", "4H"),
        "H1": ("H1", "1h", "1H"),
        "M15": ("M15", "15m", "15M"),
        "M1": ("M1", "1m", "1M"),
    }.get(timeframe, (timeframe, timeframe.lower()))
    return _get(mapping, *aliases)


def _canonical_action(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = _get(value, "decision", "action", "intent", "decision_type", "type")
    if value is None:
        return None
    action = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    if action.startswith("ENTER") or action in {"BUY", "LONG", "SHORT", "OPEN", "OPEN_LONG", "OPEN_SHORT"}:
        return "ENTER"
    if action in {"NO_TRADE", "NOTRADE", "HOLD", "SKIP"}:
        return "NO_TRADE"
    return action or None


def _intent(record: Mapping[str, Any]) -> Any:
    cycle = record.get("cycle") if isinstance(record.get("cycle"), Mapping) else {}
    return _get(record, "intent", default=_get(cycle, "intent"))


def _intent_valid(intent: Any) -> bool:
    if not isinstance(intent, Mapping):
        return False
    decision = str(_get(intent, "decision", default="")).upper()
    if decision not in {"ENTER_LONG", "ENTER_SHORT", "NO_TRADE"}:
        return False
    schema = intent.get("schema")
    if schema is not None and schema != "brooks.trade-intent.v2":
        return False
    role = intent.get("role")
    if role is not None and role != "TRADER":
        return False
    return bool(_get(intent, "symbol")) and _time_ms(_get(intent, "decision_time_ms")) is not None


def _parse_response(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _decision_from_run(run: Mapping[str, Any]) -> Any:
    for key in ("decision", "output", "model_output", "result"):
        value = run.get(key)
        if value is not None:
            return _parse_response(value)
    calls = run.get("calls")
    if isinstance(calls, list):
        for call in reversed(calls):
            if isinstance(call, Mapping) and call.get("raw_response") is not None:
                return _parse_response(call["raw_response"])
    return None


def _run_action(run: Mapping[str, Any]) -> str | None:
    output = _decision_from_run(run)
    return _canonical_action(output)


def _status_text(value: Any) -> str:
    if isinstance(value, Mapping):
        status = _get(value, "status", "outcome", "result", "state")
        if status is not None and not isinstance(status, (dict, list)):
            return str(status)
        if value.get("error"):
            return "falha"
        accepted = _get(value, "accepted", "success")
        return "aceito" if accepted is True else ("rejeitado" if accepted is False else "sem registro")
    if value is True:
        return "aceito"
    if value is False:
        return "rejeitado"
    return str(value) if value is not None else "sem registro"


def _metadata_from_first_input(run: Mapping[str, Any]) -> dict[str, Any]:
    """Read only linkage fields from the literal input sent to the role."""
    calls = run.get("calls")
    if not isinstance(calls, list) or not calls or not isinstance(calls[0], Mapping):
        return {}
    message = calls[0].get("user_message")
    if not isinstance(message, str) or "\nInput: " not in message:
        return {}
    try:
        payload = json.loads(message.split("\nInput: ", 1)[1].strip())
    except json.JSONDecodeError:
        return {}
    found: dict[str, Any] = {}

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key in ("correlation_id", "decision_time_ms", "timeframe"):
                if key in value and value[key] is not None and key not in found:
                    found[key] = value[key]
            for child in value.values():
                if isinstance(child, (Mapping, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return found


def _role_runs_for_record(
    record: Mapping[str, Any], root: Path, all_runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    refs = record.get("role_runs")
    if not isinstance(refs, list):
        refs = []
    attached: list[dict[str, Any]] = []
    record_id = _get(record, "round_id", "cycle_id")
    cycle = record.get("cycle") if isinstance(record.get("cycle"), Mapping) else {}
    target_correlation = _get(record, "correlation_id", default=_get(cycle, "correlation_id"))
    if target_correlation is None:
        symbol = _get(record, "symbol", default=_get(cycle, "symbol"))
        decision_ms = _time_ms(_get(record, "decision_time_ms", default=_get(cycle, "decision_time_ms")))
        if symbol is not None and decision_ms is not None:
            target_correlation = f"{symbol}-1h-{decision_ms}"
    by_path = {row.get("_path"): row for row in all_runs if row.get("_path")}
    by_id = {str(row.get("run_id")): row for row in all_runs if row.get("run_id") is not None}
    for ref in refs:
        if isinstance(ref, Mapping):
            path_value = _get(ref, "path", "file", "filename")
            run_id = _get(ref, "run_id", "id")
            direct = ref if any(k in ref for k in ("calls", "system", "tools")) or ("model" in ref and "role" in ref) else None
        else:
            path_value, run_id, direct = ref, None, None
        row = direct
        if row is None and path_value:
            path = _resolve(root, path_value)
            row = by_path.get(str(path)) if path else None
            if row is None and path and path.is_file():
                value, error = _load_json(path)
                if error is None and isinstance(value, dict):
                    row = dict(value)
                    row["_path"] = str(path)
        if row is None and run_id is not None:
            row = by_id.get(str(run_id))
        if row is not None:
            attached.append(row)
    if not attached:
        attached = [row for row in all_runs if str(row.get("round_id")) == str(record_id)]
    if target_correlation is not None:
        # Explicit cycle refs link the Trader record. PM files have a separate
        # scope label, so attach them through correlation_id from literal Input.
        for row in all_runs:
            if str(_get(row, "role", default="")).upper() not in {"POSITION_MANAGER", "PM"}:
                continue
            metadata = row.get("_prompt_metadata") if isinstance(row.get("_prompt_metadata"), Mapping) else {}
            if str(metadata.get("correlation_id")) == str(target_correlation):
                attached.append(row)
    # Keep unique run records without erasing distinct retry attempts.
    unique: dict[str, dict[str, Any]] = {}
    for row in attached:
        identity = str(row.get("run_id") or row.get("_path") or _sha256_bytes(_canonical_bytes(row)))
        unique.setdefault(identity, row)
    return list(unique.values())


def _load_role_runs(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    directory = root / "role_runs"
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    if not directory.exists():
        return rows, []
    for path in sorted(directory.glob("*.json")):
        value, error = _load_json(path)
        if error:
            errors.append(f"{_relative_or_name(path, root)}: {error}")
        elif isinstance(value, dict):
            value = dict(value)
            value["_path"] = str(path.resolve())
            value["_sha256"] = _sha256_bytes(path.read_bytes())
            value["_prompt_metadata"] = _metadata_from_first_input(value)
            rows.append(value)
        else:
            errors.append(f"{_relative_or_name(path, root)}: registro precisa ser objeto JSON")
    return rows, errors


def _tool_rows(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    direct = run.get("tools")
    if isinstance(direct, list):
        for tool in direct:
                    if isinstance(tool, Mapping):
                        result.append(dict(tool))
    if result:
        return result
    calls = run.get("calls")
    if isinstance(calls, list):
        for index, call in enumerate(calls):
            if not isinstance(call, Mapping):
                continue
            nested = _get(call, "tools", "tool_calls", "tool_audit", default=[])
            if isinstance(nested, list):
                for tool in nested:
                    if isinstance(tool, Mapping):
                        result.append(dict(tool))
            response = _parse_response(call.get("raw_response"))
            if not isinstance(response, Mapping) or not isinstance(response.get("tool"), str):
                continue
            tool = {
                "tool": response["tool"],
                "arguments": response.get("arguments"),
                "status": "resposta de ferramenta não localizada",
                "source": "raw_response do modelo",
            }
            if index + 1 < len(calls) and isinstance(calls[index + 1], Mapping):
                next_message = _get(calls[index + 1], "user_message", default="")
                marker = f"Read tool {response['tool']} result:"
                if isinstance(next_message, str) and marker in next_message:
                    payload = next_message.split(marker, 1)[1].split("\nContinue.", 1)[0].strip()
                    parsed_payload = _parse_response(payload)
                    tool["status"] = "falha reportada" if isinstance(parsed_payload, Mapping) and parsed_payload.get("error") else "resultado devolvido"
                    if isinstance(parsed_payload, Mapping) and parsed_payload.get("error"):
                        tool["error"] = str(parsed_payload["error"])
            result.append(tool)
    return result


def _tool_summary(run: Mapping[str, Any]) -> str:
    tools = _tool_rows(run)
    if not tools:
        return "sem chamadas registradas"
    bits = []
    for i, tool in enumerate(tools, 1):
        name = _get(tool, "name", "tool", "function", "tool_name", default="ferramenta")
        outcome = _get(tool, "status", "outcome", "error", "success")
        if outcome is None:
            outcome = "registrada"
        elif outcome is True:
            outcome = "ok"
        elif outcome is False:
            outcome = "falhou"
        bits.append(f"{i}. {name}: {outcome}")
    return "; ".join(bits)


def _gm_summary(value: Any, action: str | None = None) -> str:
    if value is None:
        return "sem evento (NO_TRADE)" if action == "NO_TRADE" else "sem registro"
    if isinstance(value, Mapping):
        event_type = _get(value, "type", "event_type", "status", "outcome")
        payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
        reason = _get(payload, "reason", "message", "error")
        binding = payload.get("binding")
        if event_type:
            detail = "binding registrado" if binding is not None else None
            if reason:
                detail = str(reason)
            return f"{event_type}" + (f" — {detail}" if detail else "")
    return _status_text(value)


def _gm_kind(value: Any, action: str | None = None) -> str:
    if value is None:
        return "sem evento (NO_TRADE)" if action == "NO_TRADE" else "sem registro"
    if isinstance(value, Mapping):
        return str(_get(value, "type", "event_type", "status", "outcome", default="resultado GM"))
    return _status_text(value)


def _role_success_status(run: Mapping[str, Any]) -> str:
    status = str(_get(run, "status", default="sem status"))
    host = run.get("host")
    if isinstance(host, Mapping) and host.get("accepted") is False:
        return "rejeitado pelo host"
    if status.lower() in {"failed", "failure", "error", "timeout"}:
        return "falha"
    return status


def _context_output(run: Mapping[str, Any]) -> Mapping[str, Any]:
    output = _decision_from_run(run)
    return output if isinstance(output, Mapping) else {}


def _context_timeframe(run: Mapping[str, Any]) -> str | None:
    output = _context_output(run)
    timeframe = _canonical_timeframe(_get(output, "timeframe", "context_timeframe"))
    if timeframe:
        return timeframe
    round_id = str(_get(run, "round_id", default="")).lower()
    if "bootstrap-1d" in round_id:
        return "D1"
    if "bootstrap-4h" in round_id:
        return "H4"
    return None


def _unwrap_context(value: Any) -> tuple[Mapping[str, Any], str | None]:
    if not isinstance(value, Mapping):
        return {}, None
    context = value.get("context") if isinstance(value.get("context"), Mapping) else value
    freshness = _get(value, "freshness", "freshness_status", "status")
    if freshness is None:
        freshness = _get(context, "freshness", "freshness_status")
    return context, str(freshness) if freshness is not None else None


def _packet_inputs(
    record: Mapping[str, Any], packet: Mapping[str, Any], root: Path, packet_path: Path | None,
    round_id: str | None,
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    raw = packet.get("raw") if isinstance(packet.get("raw"), Mapping) else {}
    macros = packet.get("macro_contexts") if isinstance(packet.get("macro_contexts"), Mapping) else {}
    refs = dict(_input_refs(record, packet))
    if packet_path:
        refs.setdefault("frozen_packet", {
            "path": _relative_or_name(packet_path, root),
            "sha256": _sha256_bytes(packet_path.read_bytes()),
        })
    summaries: dict[str, Any] = {}
    integrity: list[dict[str, Any]] = []
    for tf in ("H1", "M15"):
        payload = _mapping_alias(raw, tf)
        ref = _get(refs, tf, tf.lower(), f"raw_{tf}", f"raw{tf}", default=None)
        if ref is None:
            ref = _mapping_alias(refs, tf)
        if ref is None:
            ref = _get(raw, f"{tf}_file", f"{tf.lower()}_file", f"{tf}_path", default=_mapping_alias(raw, tf.lower() + "_file"))
        summary, detail = _source_summary(tf, payload, ref, root, count_bars=True)
        if ref is None and packet_path and payload is not None:
            packet_name = _relative_or_name(packet_path, root)
            source_link = _link(packet_path, root, f"{packet_name} (raw.{tf})")
            summary = summary.replace(f"frozen_packet.{tf} [inline]", source_link)
            if detail:
                detail["filename"] = f"{packet_name} (raw.{tf})"
                detail["path"] = packet_name
                detail["embedded_field"] = f"raw.{tf}"
        summaries[tf] = summary
        if detail:
            integrity.append({"kind": f"raw_{tf}", **detail})
    macro_rows: list[dict[str, Any]] = []
    decision_ms = _time_ms(_get(record, "decision_time_ms", default=_get(packet, "decision_time_ms")))
    for tf in ("D1", "H4"):
        value = _mapping_alias(macros, tf)
        context, freshness = _unwrap_context(value)
        ref = _get(refs, tf, tf.lower(), f"macro_{tf}", f"context_{tf}", default=None)
        if ref is None:
            ref = _mapping_alias(refs, tf)
        if ref is None:
            ref = _get(value, "file", "path", "artifact", default=None) if isinstance(value, Mapping) else None
        display, detail = _source_summary(tf, value, ref, root, count_bars=False)
        if ref is None and packet_path and value is not None:
            packet_name = _relative_or_name(packet_path, root)
            source_link = _link(packet_path, root, f"{packet_name} (macro_contexts.{tf})")
            display = display.replace(f"frozen_packet.{tf} [inline]", source_link)
            if detail:
                detail["filename"] = f"{packet_name} (macro_contexts.{tf})"
                detail["path"] = packet_name
                detail["embedded_field"] = f"macro_contexts.{tf}"
        context_ms = _time_ms(_get(context, "decision_time_ms", "generated_at_ms", "timestamp_ms"))
        lag_hours = ((decision_ms - context_ms) / 3_600_000) if decision_ms is not None and context_ms is not None else None
        macro_rows.append({
            "timeframe": tf,
            "freshness": freshness or "sem campo",
            "primary_regime": _get(context, "primary_regime", "regime"),
            "phase": context.get("phase"),
            "context_time": _get(context, "decision_time_ms", "generated_at_ms", "timestamp_ms"),
            "lag_hours": lag_hours,
            "source": display,
            "source_detail": detail,
        })
        if detail:
            integrity.append({"kind": f"macro_{tf}", **detail})
    return summaries, macro_rows, integrity


def _cycle_status(record: Mapping[str, Any]) -> str:
    cycle = record.get("cycle") if isinstance(record.get("cycle"), Mapping) else {}
    return str(_get(cycle, "status", default=_get(record, "status", default="sem status")))


def _failure_text(record: Mapping[str, Any], role_runs: list[dict[str, Any]]) -> str:
    cycle = record.get("cycle") if isinstance(record.get("cycle"), Mapping) else {}
    failure = _get(cycle, "failure", default=record.get("failure"))
    if isinstance(failure, Mapping):
        failure = _get(failure, "message", "error", "reason", default=json.dumps(failure, ensure_ascii=False))
    if failure:
        return str(failure)
    for run in role_runs:
        status = str(run.get("status", "")).lower()
        host = run.get("host")
        if status in {"failed", "failure", "error", "timeout"}:
            return str(_get(run, "error", "failure", default=f"execução {status}"))
        if isinstance(host, Mapping) and (host.get("accepted") is False or host.get("error")):
            return str(_get(host, "error", "reason", default="host rejeitou a execução"))
    if _cycle_status(record).lower() in {"failed", "failure", "error", "timeout"}:
        return _cycle_status(record)
    return ""


def _failure_type(record: Mapping[str, Any]) -> str:
    cycle = record.get("cycle") if isinstance(record.get("cycle"), Mapping) else {}
    failure = _get(cycle, "failure", default=record.get("failure"))
    if isinstance(failure, Mapping):
        return str(_get(failure, "error_type", "failure_type", "type", default="tipo não informado"))
    return str(_get(record, "failure_type", default="tipo não informado"))


def _load_simulation(root: Path, cycles: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    all_rows: dict[str, list[dict[str, Any]]] = {"trades": [], "fills": [], "equity": []}
    errors: dict[str, list[str]] = {key: [] for key in all_rows}
    sim_root = root / "simulation"
    file_names = {
        "trades": (sim_root / "trades.jsonl", root / "trades.jsonl"),
        "fills": (sim_root / "fills.jsonl", root / "fills.jsonl"),
        "equity": (sim_root / "equity.jsonl", root / "equity.jsonl"),
    }
    for kind, candidates in file_names.items():
        existing = next((p for p in candidates if p.exists()), None)
        if existing:
            rows, errs, _ = _read_jsonl(existing)
            all_rows[kind].extend(rows)
            errors[kind].extend(f"{_relative_or_name(existing, root)}: {e}" for e in errs)
    for cycle in cycles:
        sim = cycle.get("simulation")
        if not isinstance(sim, Mapping):
            continue
        for kind in all_rows:
            values = sim.get(kind)
            if isinstance(values, list):
                all_rows[kind].extend(x for x in values if isinstance(x, dict))
            elif isinstance(values, Mapping):
                all_rows[kind].append(dict(values))
    # Sidecar event logs and cycle snapshots can refer to the same event.
    for kind, rows in all_rows.items():
        seen: set[str] = set()
        unique = []
        id_fields = {
            "trades": ("trade_id", "id", "correlation_id"),
            "fills": ("fill_id", "execution_id", "id"),
            "equity": ("event_id", "time_ms", "timestamp_ms", "at_ms", "time"),
        }[kind]
        for row in rows:
            identity = _get(row, *id_fields)
            if identity is None:
                identity = _sha256_bytes(_canonical_bytes(row))
            key = str(identity)
            if key not in seen:
                seen.add(key)
                unique.append(row)
        all_rows[kind] = unique
    return all_rows, errors


def _trade_net_pnl(trade: Mapping[str, Any]) -> tuple[float | None, str | None]:
    net = _number(_get(trade, "net_pnl", "realized_net_pnl", "net_realized_pnl"))
    if net is not None:
        return net, "reportado"
    gross = _number(_get(trade, "gross_pnl", "gross_realized_pnl", "realized_gross_pnl"))
    if gross is not None:
        fees = _number(_get(trade, "fees", "fee_total")) or 0.0
        funding = _number(_get(trade, "funding", "funding_total")) or 0.0
        return gross - fees - funding, "gross_pnl − fees − funding"
    # ``realized_pnl`` can already include exchange costs depending on the
    # simulator's convention, so preserve that reported number as-is when an
    # explicitly gross/net field is unavailable.
    realized = _number(_get(trade, "realized_pnl", "pnl"))
    if realized is not None:
        return realized, "realized_pnl reportado (convenção do simulador)"
    entry = _number(_get(trade, "entry_price", "avg_entry_price"))
    exit_price = _number(_get(trade, "exit_price", "avg_exit_price"))
    quantity = _number(_get(trade, "quantity", "qty", "closed_quantity", "amount"))
    if entry is not None and exit_price is not None and quantity is not None:
        side = str(_get(trade, "side", "position_side", default="long")).lower()
        direction = -1 if side in {"short", "sell"} else 1
        gross = (exit_price - entry) * quantity * direction
        fees = _number(_get(trade, "fees", "fee_total")) or 0.0
        funding = _number(_get(trade, "funding", "funding_total")) or 0.0
        return gross - fees - funding, "derivado de preços × quantidade − custos"
    return None, None


def _is_closed(trade: Mapping[str, Any]) -> bool:
    status = str(_get(trade, "status", "lifecycle_status", "state", default="")).upper()
    if status in {"CLOSED", "CLOSE", "FLAT", "DONE", "COMPLETED", "EXITED"}:
        return True
    return bool(_get(trade, "exit_time_ms", "closed_at_ms", "exit_price", "realized_pnl", "net_pnl"))


def _trade_metrics(trades: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    closed = [trade for trade in trades if _is_closed(trade)]
    computed = []
    for trade in closed:
        net, basis = _trade_net_pnl(trade)
        if net is None:
            continue
        risk = _number(_get(trade, "initial_risk", "risk_amount", "initial_risk_amount", "initial_risk_usd"))
        r_multiple = _number(_get(trade, "r_multiple", "realized_r", "R"))
        if r_multiple is None and risk not in (None, 0):
            r_multiple = net / abs(risk)
        row = dict(trade)
        row["_net_pnl"] = net
        row["_pnl_basis"] = basis
        row["_r_multiple"] = r_multiple
        row["_mfe"] = _number(_get(trade, "mfe_r", "mfe", "max_favorable_excursion", "mfe_pnl"))
        row["_mae"] = _number(_get(trade, "mae_r", "mae", "max_adverse_excursion", "mae_pnl"))
        computed.append(row)
    pnl = [row["_net_pnl"] for row in computed]
    winners = [value for value in pnl if value > 0]
    losers = [value for value in pnl if value < 0]
    metrics = {
        "closed_trade_count": len(closed),
        "closed_trades_with_pnl_count": len(computed),
        "open_trade_count": len(trades) - len(closed),
        "win_rate": (len(winners) / len(computed)) if computed else None,
        "expectancy_net_pnl": (sum(pnl) / len(pnl)) if pnl else None,
        "net_pnl_sum": sum(pnl) if pnl else None,
        "profit_factor": (sum(winners) / abs(sum(losers))) if losers else None,
        "gross_profit": sum(winners) if pnl else None,
        "gross_loss_abs": abs(sum(losers)) if pnl else None,
        "avg_mfe": _mean([row["_mfe"] for row in computed]),
        "avg_mae": _mean([row["_mae"] for row in computed]),
        "avg_r_multiple": _mean([row["_r_multiple"] for row in computed]),
    }
    return metrics, computed


def _mean(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _drawdown(equity: list[dict[str, Any]]) -> dict[str, Any]:
    points = []
    for row in equity:
        value = _number(_get(row, "equity", "account_equity", "balance"))
        time_ms = _time_ms(_get(row, "time_ms", "timestamp_ms", "at_ms", "time", "timestamp"))
        if value is not None:
            points.append((time_ms if time_ms is not None else len(points), value))
    points.sort(key=lambda x: x[0])
    if not points:
        explicit = [_number(_get(row, "drawdown_pct", "max_drawdown_pct")) for row in equity]
        explicit = [x for x in explicit if x is not None]
        if explicit:
            return {"max_drawdown_pct": max(explicit), "max_drawdown_abs": None, "basis": "campo drawdown_pct informado"}
        return {"max_drawdown_pct": None, "max_drawdown_abs": None, "basis": None}
    peak = points[0][1]
    max_abs = 0.0
    max_pct: float | None = None
    for _, value in points:
        peak = max(peak, value)
        dd = peak - value
        max_abs = max(max_abs, dd)
        pct = dd / peak if peak > 0 else None
        if pct is not None:
            max_pct = pct if max_pct is None else max(max_pct, pct)
    return {"max_drawdown_pct": max_pct, "max_drawdown_abs": max_abs, "basis": "série de equity ordenada por timestamp"}


def _costs(trades: list[dict[str, Any]], fills: list[dict[str, Any]]) -> dict[str, Any]:
    # Prefer exchange/simulator fill costs. Fall back to trade-level totals to
    # avoid adding the same costs twice.
    source = fills if fills and any(_number(_get(x, "fee", "fees", "commission")) is not None for x in fills) else trades
    fee_by_currency: dict[str, float] = defaultdict(float)
    fee_count = 0
    for row in source:
        fee = _number(_get(row, "fee", "fees", "commission", "fee_amount"))
        if fee is not None:
            fee_count += 1
            currency = str(_get(row, "fee_currency", "fee_asset", "currency", default="unidade não informada"))
            fee_by_currency[currency] += fee
    funding = [_number(_get(row, "funding", "funding_fee", "funding_pnl")) for row in trades]
    funding = [x for x in funding if x is not None]
    return {
        "fee_source": "fills" if source is fills else "trades",
        "fee_event_count": fee_count,
        "fees_by_currency": dict(fee_by_currency),
        "funding_cost_total": sum(funding) if funding else None,
        "funding_event_count": len(funding),
    }


def _regime_groups(computed: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in computed:
        regime = _get(trade, "regime", "primary_regime", "entry_regime", "market_regime", default=trade.get("_derived_regime", "não informado"))
        groups[str(regime)].append(trade)
    result: dict[str, dict[str, Any]] = {}
    for regime, rows in sorted(groups.items()):
        pnl = [row["_net_pnl"] for row in rows]
        wins = [x for x in pnl if x > 0]
        losses = [x for x in pnl if x < 0]
        result[regime] = {
            "closed_trades_with_pnl_count": len(rows),
            "win_rate": len(wins) / len(rows) if rows else None,
            "expectancy_net_pnl": sum(pnl) / len(pnl) if rows else None,
            "net_pnl_sum": sum(pnl) if rows else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "avg_r_multiple": _mean(row.get("_r_multiple") for row in rows),
        }
    return result


def _json_clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_clean(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_json_clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def _build_rounds(root: Path, cycles: list[dict[str, Any]], all_role_runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result: list[dict[str, Any]] = []
    integrity: list[dict[str, Any]] = []
    for cycle in cycles:
        round_id = str(_get(cycle, "round_id", "cycle_id", default="sem round_id"))
        cycle_data = cycle.get("cycle") if isinstance(cycle.get("cycle"), Mapping) else {}
        packet = cycle.get("_packet") if isinstance(cycle.get("_packet"), Mapping) else {}
        packet_path = Path(cycle["_packet_path"]) if cycle.get("_packet_path") else None
        decision_ms = _time_ms(_get(cycle, "decision_time_ms", default=_get(packet, "decision_time_ms")))
        symbol = _get(cycle, "symbol", default=_get(packet, "symbol"))
        correlation_id = _get(cycle, "correlation_id", default=_get(cycle_data, "correlation_id"))
        if correlation_id is None and symbol is not None and decision_ms is not None:
            # MarketClock constructs this correlation id for H1 events; the
            # simulator stores it on fills and trades for lifecycle joins.
            correlation_id = f"{symbol}-1h-{decision_ms}"
        inputs, macros, checks = _packet_inputs(cycle, packet, root, packet_path, round_id)
        integrity.extend({"round_id": round_id, **check} for check in checks if check.get("hash_mismatch"))
        runs = cycle.get("_role_runs") or _role_runs_for_record(cycle, root, all_role_runs)
        intent = _intent(cycle)
        proposed_action = _canonical_action(intent)
        if proposed_action is None:
            for run in runs:
                if str(run.get("role", "")).upper() == "TRADER":
                    proposed_action = _run_action(run)
                    if proposed_action:
                        break
        cycle_status = _cycle_status(cycle)
        action = proposed_action if cycle_status.lower() == "completed" and _intent_valid(intent) else None
        host = _get(cycle, "host", default=None)
        if host is None:
            host = next((_get(run, "host") for run in runs if run.get("host") is not None), None)
        gm_result = _get(cycle, "gm_result", default=None)
        if gm_result is None:
            gm_result = _get(cycle_data, "gm_result", default=None)
        pm_runs = []
        context_runs = []
        role_summaries = []
        for run in runs:
            role = str(_get(run, "role", default="desconhecido"))
            if role.upper() in {"POSITION_MANAGER", "PM"}:
                pm_runs.append(run)
            if role.upper() in {"CONTEXT_ANALYST", "HTF_ANALYST", "D1", "H4"}:
                context_runs.append(run)
            path = _resolve(root, run.get("_path")) if run.get("_path") else None
            run_hash = run.get("_sha256")
            if run_hash is None and path and path.is_file():
                try:
                    run_hash = _sha256_bytes(path.read_bytes())
                except OSError:
                    pass
            role_summaries.append({
                "role": role,
                "run_id": _get(run, "run_id", "id"),
                "model": _get(run, "model", "agent_key", "model_name"),
                "status": _get(run, "status", default="sem status"),
                "action": _run_action(run),
                "tools": _tool_rows(run),
                "tool_summary": _tool_summary(run),
                "host": run.get("host"),
                "gm": _get(run, "gm", "gm_result"),
                "pm_attribution": _get(run, "pm_attribution", "attribution"),
                "lifecycle": _get(run, "lifecycle", "final_lifecycle", "position_lifecycle"),
                "file": _relative_or_name(path, root) if path else None,
                "sha256": run_hash,
            })
        cycle_tools = _get(cycle_data, "tool_audit", default=_get(cycle, "tool_audit", default=[]))
        if isinstance(cycle_tools, list) and cycle_tools:
            # The durable Trader audit is a useful source even when there is no
            # separate role-run JSON yet. It intentionally records only names,
            # arguments and outcomes, never large tool payloads.
            role_summaries.append({
                "role": "TRADER (tool_audit do ciclo)",
                "run_id": None,
                "model": None,
                "status": _cycle_status(cycle),
                "action": None,
                "tools": cycle_tools,
                "tool_summary": _tool_summary({"tools": cycle_tools}),
                "host": None,
                "gm": None,
                "pm_attribution": None,
                "lifecycle": None,
                "file": None,
                "sha256": None,
            })
        failure = _failure_text(cycle, runs)
        result.append({
            "round_id": round_id,
            "decision_time_ms": decision_ms,
            "decision_time": _fmt_time(_get(cycle, "decision_time_ms", default=_get(packet, "decision_time_ms"))),
            "symbol": symbol,
            "correlation_id": correlation_id,
            "cycle_status": cycle_status,
            "action": action,
            "proposed_action": proposed_action,
            "intent_valid": _intent_valid(intent),
            "failure_type": _failure_type(cycle) if cycle_status.lower() == "failed" else None,
            "failure": failure or None,
            "intent": intent,
            "host_acceptance": _status_text(host),
            "host": host,
            "gm_outcome": _gm_summary(gm_result, action),
            "gm_kind": _gm_kind(gm_result, action),
            "gm_result": gm_result,
            "attempt": _get(cycle_data, "attempt", default=_get(cycle, "attempt")),
            "packet_hash": _get(cycle_data, "packet_hash"),
            "raw_inputs": inputs,
            "macro_contexts": macros,
            "role_runs": role_summaries,
            "context_run_count": len(context_runs),
            "pm_run_count": len(pm_runs),
            "pm_runs": pm_runs,
            "fills": [],
            "lifecycle": _get(cycle, "lifecycle", default=_get(cycle_data, "lifecycle")),
            "simulation": cycle.get("simulation"),
            "_integrity": checks,
        })
    result.sort(key=lambda x: (x.get("decision_time_ms") or 0, x["round_id"]))
    return result, integrity


def _attach_simulation_events(
    rounds: list[dict[str, Any]], fills: list[dict[str, Any]], trades: list[dict[str, Any]],
) -> None:
    fills_by_round: dict[str, list[dict[str, Any]]] = defaultdict(list)
    trades_by_round: dict[str, list[dict[str, Any]]] = defaultdict(list)
    round_by_trade_id: dict[str, str] = {}
    round_by_correlation: dict[str, str] = {
        str(row["correlation_id"]): row["round_id"]
        for row in rounds if row.get("correlation_id") is not None
    }
    for fill in fills:
        key = _get(fill, "round_id", "cycle_id")
        if key is None:
            correlation_id = _get(fill, "correlation_id")
            key = round_by_correlation.get(str(correlation_id)) if correlation_id is not None else None
        if key is not None:
            fills_by_round[str(key)].append(fill)
    for trade in trades:
        key = _get(trade, "round_id", "cycle_id")
        correlation_id = _get(trade, "correlation_id")
        if key is None and correlation_id is not None:
            key = round_by_correlation.get(str(correlation_id))
        trade_id = _get(trade, "trade_id", "id", "correlation_id")
        if key is not None:
            trades_by_round[str(key)].append(trade)
            if trade_id is not None:
                round_by_trade_id[str(trade_id)] = str(key)
    for fill in fills:
        if _get(fill, "round_id", "cycle_id") is None:
            correlation_id = _get(fill, "correlation_id")
            directly_joined = correlation_id is not None and str(correlation_id) in round_by_correlation
            if not directly_joined:
                trade_id = _get(fill, "trade_id", "position_id", "correlation_id")
                round_id = round_by_trade_id.get(str(trade_id)) if trade_id is not None else None
                if round_id is None and trade_id is not None:
                    round_id = round_by_correlation.get(str(trade_id))
                if round_id is not None:
                    fills_by_round[round_id].append(fill)
    for row in rounds:
        row["fills"] = fills_by_round.get(row["round_id"], [])
        row["trades"] = trades_by_round.get(row["round_id"], [])


def _pm_details(round_row: Mapping[str, Any]) -> str:
    runs = round_row.get("pm_runs") or []
    details: list[str] = []
    for run in runs:
        out = _decision_from_run(run)
        action = _get(out, "action", "management_action", "decision", default="sem decisão") if isinstance(out, Mapping) else out
        attribution = _get(run, "pm_attribution", "attribution")
        lifecycle = _get(run, "lifecycle", "final_lifecycle", "position_lifecycle")
        parts = [f"{_get(run, 'status', default='sem status')}: {action}"]
        if attribution is not None:
            parts.append("atribuição " + json.dumps(attribution, ensure_ascii=False, sort_keys=True))
        if lifecycle is not None:
            parts.append("ciclo " + json.dumps(lifecycle, ensure_ascii=False, sort_keys=True))
        details.append(" — ".join(parts))
    for trade in round_row.get("trades", []):
        status = _get(trade, "status", "lifecycle_status", default="status ausente")
        exit_reason = _get(trade, "exit_reason", "close_reason", "lifecycle_outcome")
        net_pnl, basis = _trade_net_pnl(trade)
        final = f"trade {_get(trade, 'trade_id', 'id', 'correlation_id', default='sem id')}: {status}"
        exit_ms = _time_ms(_get(trade, "exit_time_ms", "closed_at_ms"))
        if exit_ms is not None:
            final += f", encerrado {_fmt_time(exit_ms)}"
        if exit_reason:
            final += f", motivo {exit_reason}"
        if net_pnl is not None:
            final += f", PnL líquido {_fmt_number(net_pnl)} ({basis})"
        actions = _get(trade, "management_actions")
        if isinstance(actions, list) and actions:
            action_names = [str(_get(action, "action", "type", default="ação")) for action in actions if isinstance(action, Mapping)]
            if action_names:
                final += ", ações PM: " + " → ".join(action_names)
        details.append(final)
    if not details:
        lifecycle = round_row.get("lifecycle")
        return "sem chamada PM" + (f"; lifecycle: {json.dumps(lifecycle, ensure_ascii=False)}" if lifecycle else "")
    return "<br>".join(_markdown_cell(value) for value in details)


def _round_role_text(round_row: Mapping[str, Any], root: Path) -> str:
    chunks = []
    for run in round_row.get("role_runs", []):
        role = _markdown_cell(run.get("role"))
        model = _markdown_cell(run.get("model") or "modelo ausente")
        status = _markdown_cell(run.get("status"))
        decision = f"; saída `{_markdown_cell(run.get('action'))}`" if run.get("action") else ""
        tools = _markdown_cell(run.get("tool_summary"))
        file = run.get("file")
        if file:
            path = root / file
            ref = _link(path, root, "JSON integral") if path.exists() else _markdown_cell(file)
        else:
            ref = "JSON não localizado"
        hash_text = f"; sha256 `{run['sha256'][:12]}…`" if run.get("sha256") else ""
        chunks.append(f"**{role}** ({model}, {status}{decision}) — ferramentas: {tools}; {ref}{hash_text}")
    return "<br>".join(chunks) if chunks else "sem registros por papel"


def _fill_summary(round_row: Mapping[str, Any]) -> str:
    fills = round_row.get("fills") or []
    if not fills:
        return "0"
    details = []
    for fill in fills:
        side = _get(fill, "side", "trade_side", "order_side", "trade_type", default="lado ausente")
        quantity = _get(fill, "quantity", "qty", "amount", "filled_quantity", "filled_amount")
        price = _get(fill, "price", "fill_price", "average_price")
        fee = _get(fill, "fee", "commission")
        currency = _get(fill, "fee_currency", "fee_asset", "currency")
        text = f"{side} {quantity if quantity is not None else '?'} @ {price if price is not None else '?'}"
        action = _get(fill, "action", "position_action")
        if action:
            text += f" ({action})"
        fill_time = _time_ms(_get(fill, "time_ms", "timestamp_ms", "at_ms"))
        if fill_time is not None:
            text += f" em {_fmt_time(fill_time)}"
        if fee is not None:
            text += f"; fee {fee}{' ' + str(currency) if currency else ''}"
        attribution = _get(fill, "attribution", "pm_attribution", "reason")
        if attribution is not None:
            text += f"; atribuição {json.dumps(attribution, ensure_ascii=False, sort_keys=True, default=str)}"
        details.append(_markdown_cell(text))
    return f"{len(fills)}: " + "<br>".join(details)


def _round_input_text(round_row: Mapping[str, Any]) -> str:
    inputs = round_row.get("raw_inputs", {})
    macros = round_row.get("macro_contexts", [])
    lines = [f"H1: {inputs.get('H1', 'sem pacote')}<br>M15: {inputs.get('M15', 'sem pacote')}"]
    for macro in macros:
        lag = f"; lag { _fmt_number(macro['lag_hours'], 2)} h" if macro.get("lag_hours") is not None else ""
        regime = f"; {macro.get('primary_regime')} / {macro.get('phase')}" if macro.get("primary_regime") else ""
        lines.append(f"{macro['timeframe']} freshness={macro['freshness']}{regime}{lag}; {macro['source']}")
    return "<br>".join(lines)


def _decision_counts(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    proposed_counts: Counter[str] = Counter()
    failures = 0
    failure_types: Counter[str] = Counter()
    for row in rounds:
        action = row.get("action")
        if action in {"ENTER", "NO_TRADE"}:
            counts[action] += 1
        elif action:
            counts["other"] += 1
        else:
            counts["sem decisão"] += 1
        proposed = row.get("proposed_action")
        if proposed in {"ENTER", "NO_TRADE"}:
            proposed_counts[proposed] += 1
        if str(row.get("cycle_status", "")).lower() == "failed":
            failures += 1
            failure_types[str(row.get("failure_type") or "tipo não informado")] += 1
    return {
        "ENTER": counts["ENTER"],
        "NO_TRADE": counts["NO_TRADE"],
        "other_actions": counts["other"],
        "without_decision": counts["sem decisão"],
        "proposed_ENTER": proposed_counts["ENTER"],
        "proposed_NO_TRADE": proposed_counts["NO_TRADE"],
        "cycles_failed": failures,
        "failure_by_type": dict(failure_types),
    }


def _role_metrics(role_runs: list[dict[str, Any]], root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    by_role: Counter[str] = Counter()
    by_role_status: dict[str, Counter[str]] = defaultdict(Counter)
    context_by_tf: dict[str, dict[str, Any]] = {}
    bootstrap: list[dict[str, Any]] = []
    context_count = 0
    pm_count = 0
    for run in role_runs:
        role = str(_get(run, "role", default="desconhecido"))
        role_upper = role.upper()
        status = _role_success_status(run)
        by_role[role] += 1
        by_role_status[role][status] += 1
        if role_upper in {"CONTEXT_ANALYST", "HTF_ANALYST", "D1", "H4"}:
            context_count += 1
            timeframe = _context_timeframe(run) or "não informado"
            group = context_by_tf.setdefault(timeframe, {"calls": 0, "by_status": {}, "bootstrap_calls": 0})
            group["calls"] += 1
            group["by_status"][status] = group["by_status"].get(status, 0) + 1
            round_id = str(_get(run, "round_id", default=""))
            if round_id.startswith("bootstrap-"):
                group["bootstrap_calls"] += 1
                output = _context_output(run)
                path = _resolve(root, run.get("_path")) if run.get("_path") else None
                bootstrap.append({
                    "round_id": round_id,
                    "timeframe": timeframe,
                    "status": status,
                    "primary_regime": _get(output, "primary_regime", "regime"),
                    "phase": output.get("phase"),
                    "decision_time_ms": _get(output, "decision_time_ms"),
                    "file": _relative_or_name(path, root) if path else None,
                    "sha256": run.get("_sha256"),
                })
        if role_upper in {"POSITION_MANAGER", "PM"}:
            pm_count += 1
    result = {
        "by_role": dict(by_role),
        "by_role_status": {role: dict(counts) for role, counts in by_role_status.items()},
        "context_calls_total": context_count,
        "context_calls_by_timeframe": context_by_tf,
        "position_manager_calls_total": pm_count,
    }
    bootstrap.sort(key=lambda row: (row.get("decision_time_ms") or 0, row["timeframe"]))
    return result, bootstrap


def _run_status(manifest: Mapping[str, Any], cycles: list[dict[str, Any]], cycle_file_exists: bool, read_errors: list[str]) -> dict[str, Any]:
    expected = _int(_get(manifest, "expected_trader_cycles", "expected_cycles", "expected_rounds"))
    declared = str(_get(manifest, "status", default="running")).lower()
    actual = len(cycles)
    if declared in {"failed", "failure", "aborted"}:
        state = "failed"
    elif expected is not None and actual >= expected and declared in {"complete", "completed", "success"}:
        state = "completed"
    elif expected is not None and actual < expected:
        state = "partial"
    elif declared in {"complete", "completed", "success"}:
        state = "completed" if not read_errors else "partial"
    elif not cycle_file_exists and actual == 0:
        state = "not_started"
    else:
        state = "partial"
    return {
        "status": state,
        "declared_status": declared,
        "expected_rounds": expected,
        "recorded_rounds": actual,
        "remaining_rounds": max(expected - actual, 0) if expected is not None else None,
        "start_ms": _get(manifest, "start_ms", "started_at_ms"),
        "end_ms": _get(manifest, "end_ms", "finished_at_ms"),
    }


def _write_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    os.replace(temp, path)


def render_report(root: Path) -> dict[str, Any]:
    """Regenerate Markdown and metrics from one walk-forward run directory."""
    root = Path(root).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise NotADirectoryError(root)
    root.mkdir(parents=True, exist_ok=True)
    manifest_value, manifest_error = _load_json(root / "run_manifest.json")
    manifest = manifest_value if isinstance(manifest_value, dict) else {}
    cycles, cycle_errors, cycle_file_exists = _read_jsonl(root / "cycles.jsonl")
    role_runs, role_errors = _load_role_runs(root)
    read_errors = []
    if manifest_error and manifest_error != "arquivo ausente":
        read_errors.append(f"run_manifest.json: {manifest_error}")
    read_errors.extend(f"cycles.jsonl: {err}" for err in cycle_errors)
    read_errors.extend(role_errors)
    for cycle in cycles:
        packet, packet_path = _find_packet(cycle, root, _get(cycle, "round_id"))
        cycle["_packet"] = packet
        cycle["_packet_path"] = str(packet_path) if packet_path else None
        cycle["_role_runs"] = _role_runs_for_record(cycle, root, role_runs)
    rounds, integrity = _build_rounds(root, cycles, role_runs)

    simulation, sim_errors = _load_simulation(root, cycles)
    for kind, errors in sim_errors.items():
        read_errors.extend(f"simulation/{kind}: {error}" for error in errors)
    _attach_simulation_events(rounds, simulation["fills"], simulation["trades"])
    round_by_correlation = {str(row["correlation_id"]): row for row in rounds if row.get("correlation_id") is not None}
    for trade in simulation["trades"]:
        if _get(trade, "regime", "primary_regime", "entry_regime", "market_regime") is not None:
            continue
        correlation = _get(trade, "correlation_id")
        round_row = round_by_correlation.get(str(correlation)) if correlation is not None else None
        if round_row:
            intent_context = _get(round_row.get("intent"), "market_context")
            macro_h4 = next((item for item in round_row.get("macro_contexts", []) if item.get("timeframe") == "H4"), {})
            trade["_derived_regime"] = _get(intent_context, "primary_regime", "regime", default=macro_h4.get("primary_regime", "não informado"))
    decisions = _decision_counts(rounds)
    trade_metrics, computed_trades = _trade_metrics(simulation["trades"])
    drawdown = _drawdown(simulation["equity"])
    costs = _costs(simulation["trades"], simulation["fills"])
    assumptions = _get(manifest, "assumptions", default={})
    funding_assumption = _get(assumptions, "funding")
    if isinstance(funding_assumption, str) and "not modeled" in funding_assumption.lower() and "zero" in funding_assumption.lower():
        costs["funding_cost_total"] = 0.0
        costs["funding_basis"] = f"assumption explícita do manifest: {funding_assumption}"
    groups = _regime_groups(computed_trades)
    role_totals, bootstrap_contexts = _role_metrics(role_runs, root)
    host_accept = sum(1 for row in rounds if row.get("host_acceptance", "").lower() in {"accepted", "aceito", "success", "completed"})
    gm_outcomes = Counter(str(row.get("gm_kind", "sem registro")) for row in rounds)
    total_fills = len(simulation["fills"])
    run_state = _run_status(manifest, cycles, cycle_file_exists, read_errors)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    metrics = {
        "schema": "brooks.walkforward-report.v1",
        "generated_at_utc": timestamp,
        "run": run_state,
        "method": {
            "classification": "replay retrospectivo de barras históricas",
            "scope": "observa decisões e resultados da simulação gravados; não valida edge prospectivo",
            "symbol": _get(manifest, "symbol", default=(rounds[0].get("symbol") if rounds else None)),
            "timeframe": _get(manifest, "timeframe", default="H1"),
            "model": _get(manifest, "model", "model_id"),
            "venue": _get(manifest, "venue", "connector"),
        "assumptions": assumptions,
        },
        "decisions": decisions,
        "role_runs": {**role_totals, "context_calls_in_rounds": sum(row["context_run_count"] for row in rounds), "position_manager_calls_in_rounds": sum(row["pm_run_count"] for row in rounds)},
        "bootstrap_contexts": bootstrap_contexts,
        "host": {"rounds_with_acceptance": host_accept, "acceptance_by_round": len(rounds), "gm_outcomes": dict(gm_outcomes)},
        "simulation": {
            "fill_count": total_fills,
            "trade_record_count": len(simulation["trades"]),
            "equity_point_count": len(simulation["equity"]),
            "trades": trade_metrics,
            "costs": costs,
            "drawdown": drawdown,
            "by_regime": groups,
        },
        "data_quality": {
            "read_errors": read_errors,
            "hash_mismatches": integrity,
            "missing_sources": [name for name, exists in (("cycles.jsonl", cycle_file_exists), ("role_runs/", (root / "role_runs").exists()), ("simulation/trades.jsonl", (root / "simulation/trades.jsonl").exists()), ("simulation/fills.jsonl", (root / "simulation/fills.jsonl").exists()), ("simulation/equity.jsonl", (root / "simulation/equity.jsonl").exists())) if not exists],
        },
        "rounds": rounds,
    }
    metrics_text = json.dumps(_json_clean(metrics), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    report_text = _render_markdown(metrics, root)
    _write_atomic(root / "metrics.json", metrics_text)
    _write_atomic(root / "REPORT.md", report_text)
    return _json_clean(metrics)


def _metric_or_na(value: Any, *, percent: bool = False) -> str:
    if value is None:
        return "N/A (sem base observável)"
    return _fmt_pct(value) if percent else _fmt_number(value)


def _render_markdown(metrics: Mapping[str, Any], root: Path) -> str:
    run = metrics["run"]
    sim = metrics["simulation"]
    trades = sim["trades"]
    costs = sim["costs"]
    funding_note = f" ({costs['funding_basis']})" if costs.get("funding_basis") else ""
    drawdown = sim["drawdown"]
    method = metrics["method"]
    output: list[str] = [
        "# Walk-forward histórico Brooks — ETH-USDT",
        "",
        f"**Status:** {run['status']} — {run['recorded_rounds']} ciclos registrados",
    ]
    if run.get("expected_rounds") is not None:
        output[-1] += f" de {run['expected_rounds']} esperados; {run['remaining_rounds']} restantes"
    output += [
        "",
        "## Método e limite de interpretação",
        "",
        "Este é um replay retrospectivo de barras históricas. Ele resume as decisões do fluxo Brooks e os resultados da simulação que foram gravados. **Não é validação prospectiva de edge** e os resultados não demonstram desempenho futuro.",
        "",
        f"- Símbolo: `{method.get('symbol') or 'não informado'}`; timeframe: `{method.get('timeframe') or 'não informado'}`.",
        f"- Modelo: `{method.get('model') or 'não informado'}`; venue/conector: `{method.get('venue') or 'não informado'}`.",
        f"- Janela manifestada: {_fmt_time(run.get('start_ms'))} até {_fmt_time(run.get('end_ms'))}.",
        f"- Relatório regenerado em `{metrics['generated_at_utc']}`. Reexecute `python scripts/brooks_walkforward_report.py --root {root}` após novos registros.",
        "",
        "## Decisões e ciclo de host",
        "",
        f"- Decisões `ENTER`: **{metrics['decisions']['ENTER']}**; `NO_TRADE`: **{metrics['decisions']['NO_TRADE']}**; outras ações: **{metrics['decisions']['other_actions']}**; sem decisão: **{metrics['decisions']['without_decision']}**.",
        f"- Ciclos `failed`: **{metrics['decisions']['cycles_failed']}**; aceitação do host: **{metrics['host']['rounds_with_acceptance']} / {metrics['host']['acceptance_by_round']}** ciclos.",
        f"- Propostas sem intent aceito: ENTER **{metrics['decisions']['proposed_ENTER']}**, NO_TRADE **{metrics['decisions']['proposed_NO_TRADE']}**; tipos de falha: `{json.dumps(metrics['decisions']['failure_by_type'], ensure_ascii=False, sort_keys=True)}`.",
        f"- Execuções de contexto (inclui bootstrap): **{metrics['role_runs']['context_calls_total']}**; Position Manager: **{metrics['role_runs']['position_manager_calls_total']}**; status por papel: `{json.dumps(metrics['role_runs']['by_role_status'], ensure_ascii=False, sort_keys=True)}`.",
        f"- Resultados GM registrados: `{json.dumps(metrics['host']['gm_outcomes'], ensure_ascii=False, sort_keys=True)}`.",
        "",
        "### Contextos macro de bootstrap",
        "",
    ]
    if metrics["bootstrap_contexts"]:
        output += ["| Timeframe | Horário | Status | Regime / fase | Evidência |", "|---|---|---|---|---|"]
        for context in metrics["bootstrap_contexts"]:
            path = root / context["file"] if context.get("file") else None
            ref = _link(path, root, "JSON completo") if path and path.exists() else "JSON não localizado"
            digest = f" sha256 `{context['sha256'][:12]}…`" if context.get("sha256") else ""
            output.append(f"| {context['timeframe']} | {_fmt_time(context.get('decision_time_ms'))} | {context['status']} | {_markdown_cell(context.get('primary_regime') or '—')} / {_markdown_cell(context.get('phase') or '—')} | {ref}{digest} |")
    else:
        output.append("Nenhuma chamada de contexto marcada como bootstrap foi capturada.")
    output += [
        "",
        "## Resultados de simulação",
        "",
    ]
    if trades["closed_trade_count"] == 0:
        output.append("Não há trades fechados registrados. Win rate, expectancy, profit factor, MFE, MAE e R ficam sem valor; nenhum resultado foi inferido a partir da decisão do modelo.")
    else:
        output.append(
            f"Trades fechados: **{trades['closed_trade_count']}** ({trades['closed_trades_with_pnl_count']} com PnL disponível); win rate: **{_metric_or_na(trades['win_rate'], percent=True)}**; expectancy líquida por trade: **{_metric_or_na(trades['expectancy_net_pnl'])}**; profit factor: **{_metric_or_na(trades['profit_factor'])}**."
        )
        output.append(
            f"PnL líquido total: **{_metric_or_na(trades['net_pnl_sum'])}**; MFE médio (R): **{_metric_or_na(trades['avg_mfe'])}**; MAE médio (R): **{_metric_or_na(trades['avg_mae'])}**; R realizado médio: **{_metric_or_na(trades['avg_r_multiple'])}**."
        )
    output += [
        f"Drawdown máximo: **{_metric_or_na(drawdown['max_drawdown_abs'])}** ({_metric_or_na(drawdown['max_drawdown_pct'], percent=True)}); base: {drawdown.get('basis') or 'série de equity ausente' }.",
        f"Custos: taxas por moeda `{json.dumps(costs['fees_by_currency'], ensure_ascii=False, sort_keys=True)}` (fonte: {costs['fee_source']}; eventos: {costs['fee_event_count']}); funding total: **{_metric_or_na(costs['funding_cost_total'])}**{funding_note}.",
        f"Fills simulados: **{sim['fill_count']}**; trades registrados: **{sim['trade_record_count']}**; pontos de equity: **{sim['equity_point_count']}**.",
        "",
        "### Resultado por regime",
        "",
    ]
    if sim["by_regime"]:
        output += [
            "| Regime | Trades fechados com PnL | Win rate | Expectancy líquida | Profit factor | R médio |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for regime, values in sim["by_regime"].items():
            output.append(
                f"| {_markdown_cell(regime)} | {values['closed_trades_with_pnl_count']} | {_metric_or_na(values['win_rate'], percent=True)} | {_metric_or_na(values['expectancy_net_pnl'])} | {_metric_or_na(values['profit_factor'])} | {_metric_or_na(values['avg_r_multiple'])} |"
            )
    else:
        output.append("Sem trades fechados com PnL e regime para agrupar.")
    output += ["", "## Ciclos horários", ""]
    if metrics["rounds"]:
        output += [
            "| Horário BRT | Round | Ciclo / decisão | Host | GM | Inputs congelados e contexto macro | Papéis, tools e JSON integral | PM, atribuição e lifecycle | Fills |",
            "|---|---|---|---|---|---|---|---|---:|",
        ]
        for row in metrics["rounds"]:
            status_action = f"{row['cycle_status']} / {row.get('action') or 'sem intent aceito'}"
            if row.get("proposed_action") and not row.get("action"):
                status_action += f"<br>proposta não contada: {row['proposed_action']}"
            if row.get("failure"):
                status_action += f"<br>falha: {_markdown_cell(row['failure'])}"
            packet_hash = row.get("packet_hash")
            if packet_hash:
                status_action += f"<br>packet `{_markdown_cell(str(packet_hash)[:12])}…`"
            role_input = _round_input_text(row)
            output.append(
                f"| {_markdown_cell(row['decision_time'])} | `{_markdown_cell(row['round_id'])}` | {_markdown_cell(status_action)} | {_markdown_cell(row['host_acceptance'])} | {_markdown_cell(row['gm_outcome'])} | {role_input} | {_round_role_text(row, root)} | {_pm_details(row)} | {_fill_summary(row)} |"
            )
    else:
        output.append("Ainda não há ciclos no arquivo `cycles.jsonl`.")
    output += ["", "## Dados faltantes e integridade", ""]
    data_quality = metrics["data_quality"]
    if data_quality["missing_sources"]:
        output.append("Fontes ausentes: " + ", ".join(f"`{x}`" for x in data_quality["missing_sources"]) + ".")
    if data_quality["read_errors"]:
        output.append("Erros/linhas incompletas lidos:")
        output.extend(f"- { _markdown_cell(err) }" for err in data_quality["read_errors"])
    if data_quality["hash_mismatches"]:
        output.append("Hashes informados que divergem dos bytes do arquivo:")
        output.extend(f"- Round `{_markdown_cell(row['round_id'])}`, `{row['kind']}`: `{row.get('expected_sha256')}` versus `{row.get('sha256')}`." for row in data_quality["hash_mismatches"])
    if not data_quality["missing_sources"] and not data_quality["read_errors"] and not data_quality["hash_mismatches"]:
        output.append("Nenhuma fonte ausente, erro de leitura ou divergência entre hash informado e arquivo foi encontrada.")
    output += [
        "",
        "Os JSONs por papel vinculados na tabela preservam o `raw_response` completo, pedido/resposta do modelo e auditoria de ferramentas. O Markdown resume o resultado para manter o relatório legível.",
        "",
    ]
    return "\n".join(output)


def _help_contract() -> str:
    return (
        "\nContrato compacto: cycles.jsonl contém um objeto por hora com round_id, decision_time_ms, symbol, status, intent, "
        "cycle{packet_hash,frozen_packet,status,failure,attempt,tool_audit}, role_runs:[caminho], gm_result e simulation. "
        "role_runs/*.json contém run_id,round_id,role,model,status,system,calls[{user_message,raw_response,started_at_ms,finished_at_ms,error,wire_request_file}],tools,host; "
        "pode incluir pm_attribution/lifecycle. Frozen packet contém macro_contexts{D1,H4} e raw{H1,M15}; os inputs podem ter refs "
        "com path/file e sha256. simulation/{trades,fills,equity}.jsonl contém eventos com identificadores, timestamps e valores de execução/equity. "
        "O manifest opcional aceita expected_trader_cycles e status. Métricas sem base observável aparecem como null/N/A."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=f"diretório da execução (padrão: {DEFAULT_ROOT})")
    parser.epilog = _help_contract()
    args = parser.parse_args(argv)
    try:
        metrics = render_report(args.root)
    except (OSError, ValueError, NotADirectoryError) as exc:
        print(f"Erro ao gerar relatório: {exc}", file=sys.stderr)
        return 2
    run = metrics["run"]
    print(
        f"Relatório atualizado: {args.root / 'REPORT.md'}; status={run['status']}; "
        f"ciclos={run['recorded_rounds']}/{run['expected_rounds'] if run['expected_rounds'] is not None else '?'}; "
        f"trades fechados={metrics['simulation']['trades']['closed_trade_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
