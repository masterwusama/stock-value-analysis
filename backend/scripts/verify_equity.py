"""Offline checks for the targeted equity repair; no database connections."""
import copy
import json
import os
import sys
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import repair_equity as repair

P, T = repair.PARENT_KEYS[0], repair.TOTAL_KEYS[0]


def legacy_row():
    return {P: 70.0, T: 85.0, '少数股东权益': 15.0, '总权益': 100.0,
            '资产总计': 180.0, '负债合计': 80.0, '原披露': '保留'}


class EquityRepairTests(unittest.TestCase):
    def test_confirmed_repair_and_idempotency(self):
        before = {'extras': legacy_row(), 'equity_parent': '70.00', 'equity_total': '85.00'}
        original = copy.deepcopy(before)
        changes, issues = repair.plan_balance(before, 'HK')
        self.assertEqual(changes['equity_parent'], Decimal('85.00'))
        self.assertEqual(changes['equity_total'], Decimal('100.00'))
        self.assertEqual(changes['extras']['原披露'], '保留')
        self.assertEqual(before, original)
        self.assertFalse(issues)
        self.assertEqual(repair.plan_balance({**before, **changes}, 'HK'), ({}, []))

    def test_alias_fills_keep_parent_and_total_separate(self):
        for key in repair.PARENT_KEYS:
            for value in (0, -10, 80):
                changes, _ = repair.plan_balance({'extras': {key: value, '股东权益合计': 100}}, 'A')
                self.assertEqual(changes['equity_parent'], Decimal(value))
                self.assertEqual(changes['equity_total'], Decimal(100))
                self.assertNotIn('extras', changes)
        changes, _ = repair.plan_balance({'extras': {'股东权益合计': 100}}, 'HK')
        self.assertNotIn('equity_parent', changes)
        self.assertEqual(changes['equity_total'], Decimal(100))

    def test_conflicts_preserve_whole_row(self):
        cases = [
            ({'extras': {**legacy_row(), '总权益': 110}}, 'HK'),
            ({'extras': {P: 80, repair.PARENT_KEYS[1]: 90, T: 100}}, 'A'),
            ({'extras': {P: 80, T: 100}, 'equity_parent': '81.00'}, 'A'),
            ({'extras': {P: True, T: 100}}, 'A'),
        ]
        for before, market in cases:
            changes, issues = repair.plan_balance(before, market)
            self.assertFalse(changes)
            self.assertTrue(any(i['reason'].startswith('conflict') for i in issues))

    def test_missing_values_do_not_clear_existing_cores(self):
        before = {'extras': {}, 'equity_parent': '80.00', 'equity_total': '100.00'}
        self.assertFalse(repair.plan_balance(before, 'HK')[0])
        before['extras'] = {P: 80.004, T: 100}
        self.assertFalse(repair.plan_balance(before, 'A')[0])

    def test_cache_only_changes_verified_hk_canonical_fields(self):
        before = {'market': 'HK', 'code': 'TEST', 'balance': [legacy_row()], 'snapshot': {'price': 10}}
        after, changes, _ = repair.plan_company(before)
        self.assertEqual(len(changes), 1)
        expected = copy.deepcopy(before)
        expected['balance'][0].update({P: 85.0, T: 100.0})
        self.assertEqual(after, expected)
        self.assertFalse(repair.plan_company(after)[1])
        for market in ('A', 'US'):
            original = {**before, 'market': market}
            self.assertEqual(repair.plan_company(original), (original, [], []))

    def test_lock_never_overwrites_existing_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / '.fetch.lock'
            lock.write_text('other-owner', encoding='ascii')
            with patch.object(repair, 'LOCK', lock):
                with self.assertRaises(FileExistsError):
                    with repair.fetch_lock():
                        self.fail('Existing lock was bypassed')
            self.assertEqual(lock.read_text(encoding='ascii'), 'other-owner')
            lock.unlink()
            with patch.object(repair, 'LOCK', lock):
                with repair.fetch_lock() as check:
                    check()
                    self.assertEqual(lock.read_text(encoding='ascii'), str(os.getpid()))
                self.assertFalse(lock.exists())

    def test_atomic_replacement_restore_and_external_change_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'TEST.json'
            before, after = b'{"before":1}', b'{"after":2}'
            path.write_bytes(before)
            meta = repair.metadata(path.stat())
            item = {'name': path.name, 'before_sha256': repair.digest(before),
                    'after_sha256': repair.digest(after), 'metadata': meta}
            archive_path = Path(directory) / 'backup.zip'
            with zipfile.ZipFile(archive_path, 'w') as archive:
                archive.writestr('before/' + path.name, before)
            with zipfile.ZipFile(archive_path) as archive, patch.object(repair, 'COMPANIES', Path(directory)):
                temp = repair.stage_file(path, after, meta)
                info = temp.stat()
                attempts = [(item, (info.st_dev, info.st_ino))]
                os.replace(temp, path)
                self.assertEqual(path.stat().st_mtime_ns, meta['mtime_ns'])
                self.assertFalse(repair.restore_changed(archive, attempts))
                self.assertEqual(path.read_bytes(), before)
                temp = repair.stage_file(path, after, meta)
                info = temp.stat()
                os.replace(temp, path)
                path.write_bytes(b'{"external":3}')
                self.assertTrue(repair.restore_changed(archive, [(item, (info.st_dev, info.st_ino))]))
                self.assertEqual(json.loads(path.read_bytes()), {'external': 3})


if __name__ == '__main__':
    unittest.main()
