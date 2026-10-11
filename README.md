# Стартовий проєкт APSIK-P03

1. Відкрийте проєкт у GitHub Codespaces (Code → Codespaces → Create codespace): залежності, зокрема `ansible-core`, встановляться автоматично. Локально на Linux або в WSL2: `python3 -m venv .venv`, `source .venv/bin/activate`, `pip install -r requirements.txt`.
2. Визначте задачу й набір за номером варіанта: задача = `(номер - 1) // 3 + 1`, набір = `(номер - 1) % 3 + 1`. Реалізуйте функцію у файлі `variants/variant_NN.py` (NN це номер задачі) за карткою.
3. Запускайте відкриті тести своєї задачі: `pytest tests/test_variant_NN.py` (лише свій набір: додайте `-k S2`). Права адміністратора не потрібні: усі запуски Ansible йдуть на localhost у тимчасовому каталозі.
4. Додайте у `tests/test_my.py` власні перевірки та створіть `ANSWERS.md` (див. розділ «Що і як здавати» документа).
5. Змінюйте лише свій файл у `variants/`, файл `tests/test_my.py` і файл `ANSWERS.md`.
