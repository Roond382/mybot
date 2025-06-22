import os
import asyncio
from bot import main

if __name__ == "__main__":
    # Проверяем рабочее время перед запуском
    if os.getenv("PYTHONANYWHERE_SITE"):
        # На PythonAnywhere используем обычный запуск
        main()
    else:
        # Локальный запуск
        main()