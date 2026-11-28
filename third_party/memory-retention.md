# Memory retention dependencies

FreeAI ships these Python modules with the native Windows app for offline memory retention. The files are copied byte for byte from published PyPI wheels. Only the Python modules, type marker, and licenses are distributed.

| Package | Version | License | Files |
| --- | --- | --- | --- |
| [Py-FSRS](https://github.com/open-spaced-repetition/py-fsrs/tree/v6.3.3) | 6.3.3 | MIT | `fsrs/` and [license](fsrs-LICENSE) |
| [typing-extensions](https://github.com/python/typing_extensions/tree/4.16.0) | 4.16.0 | PSF-2.0 | `typing_extensions.py` and [license](typing_extensions-LICENSE) |

| PyPI release file | SHA-256 |
| --- | --- |
| [fsrs-6.3.3-py3-none-any.whl](https://pypi.org/project/fsrs/6.3.3/#files) | `0e06323c564fdb429bf65f8d0d3d5ace05b19e005ac080f015dc8631ad3901e2` |
| [typing_extensions-4.16.0-py3-none-any.whl](https://pypi.org/project/typing-extensions/4.16.0/#files) | `481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8` |

Py-FSRS estimates human flashcard recall from review outcomes. FreeAI uses its score as a heuristic for aging assistant memories. Importance is an application setting. FreeAI uses the scheduler and card API with the default model parameters.

With the default parameters, a memory crosses 0.60 about 63 days after its initial review if it receives no further reviews. An important memory uses 0.45 and crosses it at about 416 days. Later reviews change these estimates. The application checks a minimum age of 30 days before archival. Pinned, manually saved, and user-verified records are protected from automatic archival.
