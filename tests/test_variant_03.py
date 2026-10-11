"""Відкриті тести задачі 3: набори даних S1, S2, S3 з карток варіантів.

Ідентифікатор тесту: T03-<набір>-<перевірка>. Для свого набору: pytest tests/test_variant_03.py -k S2
Перевірки з позначкою real запускають Ansible на localhost у тимчасовому каталозі; без ansible-core вони пропускаються.
"""
import pytest
from conftest import load

import lablib


@pytest.mark.parametrize("n,ds,name", lablib.case_params(3))
def test_task_03(n, ds, name, tmp_path):
    lablib.run_check(load(n), n, ds, name, tmp_path)
