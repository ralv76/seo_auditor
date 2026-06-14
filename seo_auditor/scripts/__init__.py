# scripts/ — расширяемые анализаторы (плагины)
# Каждый файл должен реализовывать функцию:
#   analyze(raw_data: list, cfg: dict) -> dict
# Результат добавляется в aggregated['plugins'][module_name]
