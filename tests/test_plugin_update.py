import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("update_local_plugin", Path(__file__).resolve().parents[1] / "tools/update_local_plugin.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class CacheUpdateTests(unittest.TestCase):
    def test_success_and_failure_restore_old_paths_without_overwriting_new_version(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                cache = Path(directory)
                old = cache / "old"
                old.mkdir()
                (old / "record_event.py").write_text("original recorder")
                try:
                    with updater.preserve_cache(cache):
                        shutil.rmtree(old)
                        (cache / "new").mkdir()
                        (cache / "new/record_event.py").write_text("new recorder")
                        if fail:
                            raise RuntimeError("installer failed after cache cleanup")
                except RuntimeError:
                    pass
                self.assertEqual((old / "record_event.py").read_text(), "original recorder")
                self.assertEqual((cache / "new/record_event.py").read_text(), "new recorder")
