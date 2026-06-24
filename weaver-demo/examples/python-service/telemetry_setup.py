"""OTel SDK 初始化，將訊號發往 OTLP endpoint（Weaver live-check 或 OTel Collector）"""

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup(service_name: str, otlp_endpoint: str) -> tuple:
    """
    初始化 OTel SDK。
    回傳 (tracer_provider, meter_provider) 供手動 shutdown 使用。
    """
    resource = Resource(
        attributes={
            SERVICE_NAME: service_name,
            SERVICE_VERSION: "v1.0.0",
        }
    )

    # Trace
    trace_exporter = OTLPSpanExporter(
        endpoint=otlp_endpoint,
        insecure=True,
    )
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(trace_exporter))
    trace.set_tracer_provider(tp)

    # Metrics
    metric_exporter = OTLPMetricExporter(
        endpoint=otlp_endpoint,
        insecure=True,
    )
    reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=5000)
    mp = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(mp)

    return tp, mp


def shutdown(tp, mp) -> None:
    tp.force_flush()
    tp.shutdown()
    mp.shutdown()
