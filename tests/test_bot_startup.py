import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class BotStartupTest(unittest.TestCase):
    def test_bot_registers_plugins_without_connecting(self):
        # Use an isolated interpreter because bot.py configures process-wide logging.
        # No production credentials or database configuration are needed for this test.
        script = textwrap.dedent('''
            from unittest.mock import patch

            import dns.resolver  # Required for mongodb+srv connection strings.
            import psycopg2     # Also ship the optional PostgreSQL driver.

            with patch("logging.config.fileConfig"):
                from bot import Bot

            bot = Bot()
            with patch.object(bot, "add_handler") as add_handler:
                bot.load_plugins()
                assert add_handler.called, "no Telegram plugins registered their handlers"
        ''')
        result = subprocess.run(
            [sys.executable, '-c', script],
            cwd=Path(__file__).resolve().parents[1],
            env={
                **{key: os.environ[key] for key in ('PATH', 'SYSTEMROOT') if key in os.environ},
                'PYTHONDONTWRITEBYTECODE': '1',
                'API_ID': '12345',
                'DATABASE_URI': 'mongodb://127.0.0.1:27017/?connect=false',
            },
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
