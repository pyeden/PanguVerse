"""场景规格层：YAML/JSON 加载与校验。"""

from .loader import TestSuite, load_suite, load_suite_dict, load_suite_text
from .miniyaml import MiniYamlError
from .miniyaml import load as load_yaml_text

__all__ = [
    "MiniYamlError",
    "TestSuite",
    "load_suite",
    "load_suite_dict",
    "load_suite_text",
    "load_yaml_text",
]
