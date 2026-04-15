from pathlib import Path

from pyframework_pipeline.validators.four_layer import ValidationReport, validate_four_layer_project


def run(path: Path) -> ValidationReport:
    return validate_four_layer_project(path)
