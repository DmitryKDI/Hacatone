# Hacatone

## Инструменты разработки

Для установки в облачной среде Linux нужен Node.js 22.20.0 или новее.

```bash
./scripts/install-dev-tools.sh
export PATH="/workspace/.local/bin:$PATH"
export OPENSPEC_TELEMETRY=0
```

- `openspec list` показывает активные изменения OpenSpec. Для подготовки нового изменения используйте навык `$openspec-propose` в Codex.
- `rtk git status` выводит краткий статус Git. RTK можно вызывать явно из терминала.
- `deslop-please` проверяет архитектуру Rust-проекта. Для проверки нужны Cargo и рабочая область Rust.

По умолчанию команды устанавливаются в `/workspace/.local/bin`. Задайте `DEV_TOOLS_PREFIX` перед установкой, чтобы выбрать другой каталог, и добавьте его подкаталог `bin` в `PATH`.
