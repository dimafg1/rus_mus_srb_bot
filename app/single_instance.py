# app/single_instance.py
"""Защита от двух одновременно запущенных процессов бота.

FSM-замки (app/fsm_storage.py) внутрипроцессные: два параллельных процесса
бота работали бы с одной SQLite, но каждый — со своим независимым набором
asyncio.Lock, и параллельная запись одного и того же черновика могла бы
портить данные мастера друг у друга. systemd на сервере обычно не даёт
запустить второй экземпляр юнита, но это не страхует от случайного
`python app/main.py` вручную поверх уже работающего процесса (например,
после ручного restart_bot.sh кто-то забыл, что бот уже запущен) —
файловая блокировка страхует оба случая.
"""

import fcntl
import os
import sys

_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.lock"
)

_lock_file = None  # держим дескриптор живым на весь процесс — иначе GC снимет flock


def acquire_or_exit() -> None:
    """Захватить эксклюзивную файловую блокировку или завершить процесс с понятным сообщением."""
    global _lock_file
    f = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"Бот уже запущен (файл блокировки занят: {_LOCK_PATH}).\n"
            f"Повторный запуск отменён — второй процесс сломал бы FSM-состояния "
            f"пользователей, находящихся в середине мастера.",
            file=sys.stderr,
        )
        f.close()
        sys.exit(1)
    f.write(str(os.getpid()))
    f.flush()
    _lock_file = f
