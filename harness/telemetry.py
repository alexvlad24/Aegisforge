"""OpenTelemetry tracing → Phoenix (SPEC §2, §7 Week 2).

One trace per run. The root span `aegis.run` is emitted (and exported) at RunStarted; its trace
and span ids are stored in that event. Every later span (model turns, tool calls, resume and
finish markers) is a child of that root through a remote span context rebuilt from the event, so
a run that was killed and resumed in a new process still forms a single trace.

Spans are exported synchronously (SimpleSpanProcessor): a process killed mid-run loses at most
the span that was open, never earlier ones.

Attributes follow the OTel GenAI semantic conventions (gen_ai.*) plus OpenInference span kinds,
which Phoenix uses for display. No message content is put on spans.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import (
    NonRecordingSpan,
    Span,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
)

PHOENIX_ENDPOINT = os.environ.get("AEGIS_PHOENIX_URL", "http://127.0.0.1:6006")
PHOENIX_PROJECT = "aegisforge"
TRACER_NAME = "aegis.harness"

AttrValue = str | bool | int | float


def phoenix_exporter(base_url: str = PHOENIX_ENDPOINT) -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=f"{base_url}/v1/traces", timeout=5)


class RunTracer:
    """Spans for one run. `exporter=None` disables export (spans are still created, no-op)."""

    def __init__(self, exporter: SpanExporter | None) -> None:
        self._provider = TracerProvider(
            resource=Resource.create(
                {"service.name": "aegis-harness", "openinference.project.name": PHOENIX_PROJECT}
            )
        )
        if exporter is not None:
            self._provider.add_span_processor(SimpleSpanProcessor(exporter))
        self._tracer = self._provider.get_tracer(TRACER_NAME)
        self._parent: Context | None = None

    def start_run(self, attrs: dict[str, AttrValue]) -> tuple[str, str]:
        """Emit the root span and return (trace_id_hex, span_id_hex) for RunStarted."""
        span = self._tracer.start_span("aegis.run", attributes=_with_kind("AGENT", attrs))
        ctx = span.get_span_context()
        span.end()
        trace_id, span_id = f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
        self.attach(trace_id, span_id)
        return trace_id, span_id

    def attach(self, trace_id: str, root_span_id: str) -> None:
        """Parent all further spans under the run's root span (also used after a resume)."""
        root = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(root_span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        self._parent = trace.set_span_in_context(NonRecordingSpan(root))

    @contextmanager
    def span(self, name: str, kind: str, attrs: dict[str, AttrValue]) -> Iterator[Span]:
        if self._parent is None:
            raise RuntimeError("RunTracer used before start_run/attach")
        with self._tracer.start_as_current_span(
            name,
            context=self._parent,
            attributes=_with_kind(kind, attrs),
            record_exception=True,
            set_status_on_exception=True,
        ) as span:
            yield span

    def marker(self, name: str, attrs: dict[str, AttrValue]) -> None:
        with self.span(name, "CHAIN", attrs):
            pass

    def shutdown(self) -> None:
        # Flush only: the exporter belongs to the caller (Services) and may serve later runs in
        # the same process (resume, replay); provider.shutdown() would shut it down too.
        self._provider.force_flush()


def _with_kind(kind: str, attrs: dict[str, AttrValue]) -> dict[str, AttrValue]:
    return {"openinference.span.kind": kind, **attrs}


def set_attrs(span: Span, attrs: dict[str, Any]) -> None:
    for key, value in attrs.items():
        if value is not None:
            span.set_attribute(key, value)


def mark_error(span: Span, message: str) -> None:
    span.set_status(Status(StatusCode.ERROR, message))
