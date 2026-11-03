import importlib.util
import json
import contextlib
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("update_local_plugin", Path(__file__).resolve().parents[1] / "tools/update_local_plugin.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class CacheUpdateTests(unittest.TestCase):
    def test_install_does_not_depend_on_host_skill_and_restores_public_manifest(self):
        for fail in (False, True):
            with self.subTest(fail=fail),tempfile.TemporaryDirectory() as directory:
                home=Path(directory);repo=home/'repo';source=repo/'plugins/navi';source.mkdir(parents=True)
                manifest=source/'.codex-plugin/plugin.json';manifest.parent.mkdir()
                original=b'{"name":"navi","version":"0.1.0-alpha.1"}\n';manifest.write_bytes(original)
                marketplace=home/'.agents/plugins/marketplace.json';marketplace.parent.mkdir(parents=True)
                marketplace.write_text(json.dumps({'name':'personal','plugins':[{'name':'navi','source':{'source':'local','path':str(source)}}]}))
                cache=home/'.codex/plugins/cache/personal/navi'
                def install(*args,**kwargs):
                    if fail:raise RuntimeError('installer failed')
                    version=json.loads(manifest.read_text())['version'];shutil.copytree(source,cache/version)
                    return type('Result',(),{'returncode':0})()
                with patch.object(updater,'__file__',str(repo/'tools/update_local_plugin.py')),\
                     patch.object(updater.Path,'home',return_value=home),\
                     patch.object(updater.subprocess,'run',side_effect=install),\
                     patch.object(updater.sys,'argv',['update_local_plugin.py']),\
                     patch.dict(updater.os.environ,{'CODEX_HOME':str(home/'.codex')}),\
                     contextlib.redirect_stdout(io.StringIO()):
                    if fail:
                        with self.assertRaises(RuntimeError):updater.main()
                    else:updater.main()
                self.assertEqual(manifest.read_bytes(),original)

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
