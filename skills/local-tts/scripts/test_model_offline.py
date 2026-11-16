import argparse
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import model_preflight as preflight
from app.engines.cosyvoice.engine import CosyVoice3Engine
import tts_cli


def fixture(root, version):
    names = [f'{version}.yaml', 'llm.pt', 'flow.pt', 'hift.pt', 'campplus.onnx',
             f'speech_tokenizer_v{version[-1]}.onnx']
    names += [f'CosyVoice-BlankEN/{name}' for name in
              ('config.json', 'model.safetensors', 'tokenizer_config.json', 'vocab.json', 'merges.txt')]
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
    return names


class OfflineTests(unittest.TestCase):
    def test_preflight_both_versions_and_missing_empty(self):
        for version in ('cosyvoice2', 'cosyvoice3'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                names = fixture(root, version)
                self.assertEqual(preflight.validate_model(root, version), root.resolve())
                for name in names:
                    path = root / name
                    path.unlink()
                    with self.assertRaisesRegex(ValueError, '缺少文件或文件为空'):
                        preflight.validate_model(root, version)
                    path.write_bytes(b'')
                    with self.assertRaisesRegex(ValueError, '缺少文件或文件为空'):
                        preflight.validate_model(root, version)
                    path.write_bytes(b'x')
                (root / 'cosyvoice.yaml').write_text('x')
                with self.assertRaisesRegex(ValueError, '布局冲突'):
                    preflight.validate_model(root, version)
        with self.assertRaisesRegex(ValueError, '模型目录'):
            preflight.validate_model('/nonexistent/tts-model', 'cosyvoice3')

    def test_environment_before_import_and_restore(self):
        with patch.dict(os.environ, {'HF_HUB_OFFLINE': '0'}, clear=False):
            with preflight.offline_environment():
                self.assertEqual(os.environ['HF_HUB_OFFLINE'], '1')
                self.assertEqual(os.environ['TRANSFORMERS_OFFLINE'], '1')
                preflight.assert_offline_ready()
            self.assertEqual(os.environ['HF_HUB_OFFLINE'], '0')
            with self.assertRaises(RuntimeError):
                preflight.assert_offline_ready()
        for name in ('huggingface_hub.constants', 'transformers'):
            with patch.dict(sys.modules, {name: types.ModuleType(name)}):
                with self.assertRaisesRegex(RuntimeError, '已导入'):
                    with preflight.offline_environment():
                        pass

    def test_snapshot_binding_restored_after_error(self):
        original = lambda *args: 'network'
        module = types.SimpleNamespace(snapshot_download=original)
        with self.assertRaisesRegex(RuntimeError, '网络回退'):
            with preflight.block_snapshot_download(module):
                module.snapshot_download('missing')
        self.assertIs(module.snapshot_download, original)
        with self.assertRaisesRegex(RuntimeError, '无法保护'):
            with preflight.block_snapshot_download(types.SimpleNamespace()):
                pass

    def test_engine_guards_actual_bound_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root, 'cosyvoice3')
            engine = CosyVoice3Engine(model_dir=str(root))
            engine.offline = True
            original = lambda *args: 'network'
            cli = types.ModuleType('cosyvoice.cli.cosyvoice')
            cli.snapshot_download = original
            def auto(**kwargs):
                self.assertEqual(os.environ['HF_HUB_OFFLINE'], '1')
                return cli.snapshot_download('missing')
            cli.AutoModel = auto
            package = types.ModuleType('cosyvoice')
            package.__path__ = []
            sub = types.ModuleType('cosyvoice.cli')
            sub.__path__ = []
            sub.cosyvoice = cli
            package.cli = sub
            fake_modules = {'cosyvoice': package, 'cosyvoice.cli': sub,
                            'cosyvoice.cli.cosyvoice': cli}
            with patch.dict(sys.modules, fake_modules), \
                 patch('app.engines.cosyvoice.engine._install_paths'), \
                 patch('app.engines.cosyvoice.engine._patch_load_wav'), \
                 preflight.offline_environment():
                with self.assertRaisesRegex(RuntimeError, '网络回退'):
                    engine.load()
            self.assertIs(cli.snapshot_download, original)
            self.assertFalse(engine.is_loaded())

    def test_cli_offline_scopes_engine_and_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root, 'cosyvoice3')
            engine = CosyVoice3Engine(model_dir=str(root))
            args = argparse.Namespace(command='synth', offline=True, model='cosyvoice3')
            config = {'engines': {'cosyvoice3': {'model_path': str(root)}}}
            def fake_run(_args):
                self.assertTrue(engine.offline)
                self.assertEqual(os.environ['HF_HUB_OFFLINE'], '1')
                return {'output': 'fixture'}
            with patch('tts_cli.load_config', return_value=config), \
                 patch('tts_cli.engine_for', return_value=engine), \
                 patch('tts_cli._run', side_effect=fake_run):
                self.assertEqual(tts_cli.run(args), {'output': 'fixture'})
                self.assertFalse(engine.offline)
                engine._loaded = True
                with self.assertRaisesRegex(ValueError, '已加载'):
                    tts_cli.run(args)

    def test_cli_offline_requires_configured_model(self):
        args = argparse.Namespace(command='synth', offline=True, model='cosyvoice3')
        with patch('tts_cli.load_config', return_value={'engines': {'cosyvoice3': {}}}):
            with self.assertRaisesRegex(ValueError, '配置中指定'):
                tts_cli.run(args)


if __name__ == '__main__':
    unittest.main()
