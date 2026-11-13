"""One JSON object a line on standard output, for every container: the solver's uvicorn
(`logging.json` here), and `web` and `worker`, whose settings and gunicorn config use the same formatter. Here because the
backend already imports `solver_service` and the solver cannot import the backend.

What a line holds is what its caller logged: the worker's can name a nurse and a date (a solver's error, a refusal's words), so
these logs are personal data."""
from __future__ import annotations

import datetime as dt
import json
import logging

# What every LogRecord has; anything else on one was added by its caller (`extra=`) and is kept.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"time": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                 "level": record.levelname, "logger": record.name, "message": self.message_of(record)}
        entry |= self.fields_of(record)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, default=str, ensure_ascii=False)

    def message_of(self, record: logging.LogRecord) -> str:
        return record.getMessage()

    def fields_of(self, record: logging.LogRecord) -> dict:
        return {key: value for key, value in vars(record).items() if key not in _STANDARD}


class AccessFormatter(JsonFormatter):
    """Gunicorn's access log, whose record carries the request as gunicorn's atoms: the same fields every line."""

    def message_of(self, record: logging.LogRecord) -> str:
        atoms = record.args if isinstance(record.args, dict) else {}
        return f"{atoms.get('m', '-')} {atoms.get('U', '-')} {atoms.get('s', '-')}" if atoms else record.getMessage()

    def fields_of(self, record: logging.LogRecord) -> dict:
        atoms = record.args if isinstance(record.args, dict) else {}
        if not atoms:
            return super().fields_of(record)
        # The client's address as Caddy worked it out (backend/audit/log.py, `client_ip`), else the peer.
        client_address = atoms.get("{x-client-ip}i", "-")
        return {"method": atoms.get("m"), "path": atoms.get("U"), "query": atoms.get("q") or None,
                "status": _number(atoms.get("s")), "duration_ms": _number(atoms.get("M")), "bytes": _number(atoms.get("B")),
                "remote": client_address if client_address not in ("", "-") else atoms.get("h")}


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
