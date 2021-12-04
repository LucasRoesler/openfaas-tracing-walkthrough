from typing import Optional, Union
from flask import Flask, request
from flask.logging import default_handler
from flask.typing import StatusCode
import requests
from urllib.parse import urlparse

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.trace.status import StatusCode, Status

import logging
import os

from werkzeug.datastructures import MultiDict

TRUTHY = ["true", "t", "1"]
# use logfmt for our logging
LOG_FMT = 'time="%(asctime)s" service=%(name)s level=%(levelname)s %(message)s trace_id=%(trace_id)s'

# allow overridding the service name
NAME = os.getenv("SERVICE_NAME", "is-it-down")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


# QueryArgs is the type used for flask query parameters
QueryArgs = MultiDict[str, str]


class SpanFormatter(logging.Formatter):
    """log formatter that is aware of the current trace id"""

    def format(self, record):
        trace_id = trace.get_current_span().get_span_context().trace_id
        if trace_id == 0:
            record.trace_id = None
        else:
            record.trace_id = "{trace:032x}".format(trace=trace_id)
        return super().format(record)


resource = Resource(attributes={SERVICE_NAME: NAME})
provider = TracerProvider(resource=resource)

if os.getenv("TRACING", "false").lower() in TRUTHY:
    # configure via the OTEL_EXPORTER_OTLP_* env variables
    # see https://opentelemetry-python.readthedocs.io/en/latest/sdk/environment_variables.html#opentelemetry-sdk-environment-variables
    exporter = OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))

trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)


def initialize(app: Flask) -> Flask:
    FlaskInstrumentor().instrument_app(app)
    RequestsInstrumentor().instrument()

    app.logger.setLevel(LOG_LEVEL)

    default_handler.setFormatter(SpanFormatter(LOG_FMT))

    # hook to log requests
    def log_request(response):
        app.logger.info(
            'addr="%s" method=%s scheme=%s path="%s" status=%s',
            request.remote_addr,
            request.method,
            request.scheme,
            request.full_path,
            response.status_code,
        )
        return response

    app.after_request(log_request)

    return app


def handle(req: Union[str, QueryArgs]):
    """handle a request to the function
    Args:
        req (str): request body
    """
    with tracer.start_as_current_span("handle") as span:
        url = ""
        if isinstance(req, str):
            url = req
        else:
            url = req.get("url", "")

        url = url.strip()

        span.set_attribute("url", url)

        if not valid_uri(url):
            msg = f'invalid or empty url: "{url}"'
            span.set_status(Status(status_code=StatusCode.ERROR, description=msg))
            return msg, 409

        try:
            r = requests.get(url)
        except requests.exceptions.ConnectionError as exc:
            span.record_exception(exc)
            span.set_attribute("response", "down")
            return "down"

        if r.status_code > 399:
            span.set_attribute("response", "down")
            return "down"

        span.set_attribute("response", "up")
        return "up"


def valid_uri(value: Optional[str]):
    if value is None:
        return False

    try:
        result = urlparse(value)
        return all([result.scheme, result.netloc])
    except:
        return False
