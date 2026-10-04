import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from openpyxl import Workbook
from evaluation_judger.inspection import install_tool
from evaluation_judger import inspection_mcp


class NativeInspectorTransport(TestCase):
    def test_stdio_handshake_queries_and_access_denial(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            wb = Workbook(); wb.active.append(['value']); wb.active.append([8]); wb.active.append([12])
            wb.save(root / 'data.xlsx'); wb.close()
            (root / 'manifest.json').write_text(json.dumps({'files': [{'category': 'sources', 'file': 'data.xlsx'}]}))
            messages = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'inspector', 'arguments': {'request': json.dumps({'file': 'data.xlsx', 'operation': 'stats', 'columns': ['value']})}}},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'inspector', 'arguments': {'request': json.dumps({'file': '../secret.xlsx', 'operation': 'summary'})}}},
                {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'shell', 'arguments': {'command': 'anything'}}},
            ]
            result = subprocess.run([sys.executable, '-X', 'utf8', inspection_mcp.__file__, '--workspace', str(root)], input='\n'.join(map(json.dumps, messages))+'\n', capture_output=True, text=True, encoding='utf-8', timeout=20, check=True)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([r['id'] for r in replies], [1, 2, 3, 4, 5])
            self.assertEqual(replies[0]['result']['protocolVersion'], '2025-03-26')
            self.assertEqual(replies[1]['result']['tools'][0]['name'], 'inspector')
            self.assertEqual(json.loads(replies[2]['result']['content'][0]['text'])['columns']['value']['sum'], 20)
            self.assertTrue(replies[3]['result']['isError'])
            self.assertEqual(replies[4]['error']['code'], -32602)

    def test_installer_preserves_permissions_and_archives_legacy_wrapper(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); config = {'agent': {'judge': {'permission': {'read': {'*': 'deny', 'own/*': 'allow'}}}}}
            (root / 'opencode.jsonc').write_text(json.dumps(config))
            legacy = root / '.opencode/tools/material_inspector.ts'; legacy.parent.mkdir(parents=True); legacy.write_text('old wrapper')
            install_tool(root); install_tool(root)
            updated = json.loads((root / 'opencode.jsonc').read_text())
            self.assertEqual(updated['agent'], config['agent'])
            self.assertEqual(updated['mcp']['material']['command'][-1], str(root.resolve()))
            self.assertFalse(legacy.exists())
            self.assertEqual(legacy.with_suffix('.ts.disabled').read_text(), 'old wrapper')
