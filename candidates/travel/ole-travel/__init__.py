"""Native Hermes registration; model input cannot supply sources, frames or scripts."""
from pathlib import Path
import importlib.util


def register(ctx):
    source=Path(__file__).with_name("runtime.py")
    spec=importlib.util.spec_from_file_location(__name__+".native_runtime",source)
    runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
    runtime.register(ctx)
