"""
Convert the MySQL/MariaDB database and every table to utf8mb4.

The connection already asks for utf8mb4 (config/settings/prod.py), but that only
governs the conversation with the server. Tables take the character set of the
database they were created in, and a cPanel database is created latin1 by
default — so names such as "Ìmọ̀lè" or "Adéọlá" are refused with
``Incorrect string value``, and a member with Yorùbá, Igbo or Hausa tone marks in
their name cannot sign up.

    python manage.py fix_db_charset            # report only
    python manage.py fix_db_charset --apply    # convert

Take a backup first (cPanel → Backup, or mysqldump). Converting rewrites each
table; existing text is converted, not lost, but this is the live database.
Safe to re-run: tables already in utf8mb4 are skipped.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

CHARSET = "utf8mb4"
COLLATION = "utf8mb4_unicode_ci"


class Command(BaseCommand):
    help = "Report, and with --apply convert, tables that are not utf8mb4."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Convert the database default and every non-utf8mb4 table.")

    def handle(self, *args, **options):
        if connection.vendor != "mysql":
            raise CommandError(
                f"This database is {connection.vendor}, not MySQL/MariaDB; "
                f"there is nothing to convert.")

        with connection.cursor() as cur:
            cur.execute("SELECT DATABASE()")
            (name,) = cur.fetchone()
            cur.execute(
                "SELECT DEFAULT_CHARACTER_SET_NAME FROM "
                "information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", [name])
            (default,) = cur.fetchone()
            # A table's collation names its character set ("latin1_…").
            cur.execute(
                "SELECT TABLE_NAME, TABLE_COLLATION FROM "
                "information_schema.TABLES WHERE TABLE_SCHEMA = %s "
                "AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME", [name])
            tables = [(t, c or "") for t, c in cur.fetchall()]

        wrong = [(t, c) for t, c in tables if not c.startswith(CHARSET)]
        self.stdout.write(f"Database {name}: default character set {default}")
        self.stdout.write(
            f"{len(wrong)} of {len(tables)} tables are not {CHARSET}.")
        for table, collation in wrong:
            self.stdout.write(f"  {table}  ({collation})")

        if not options["apply"]:
            if wrong or default != CHARSET:
                self.stdout.write(self.style.WARNING(
                    "Report only. Back up the database, then re-run with "
                    "--apply to convert."))
            else:
                self.stdout.write(self.style.SUCCESS("Nothing to convert."))
            return

        with connection.cursor() as cur:
            # New tables (future migrations) are then created in utf8mb4 too.
            cur.execute(f"ALTER DATABASE `{name}` CHARACTER SET {CHARSET} "
                        f"COLLATE {COLLATION}")
            # Foreign keys between text columns would otherwise refuse a
            # conversion that briefly leaves the two sides different.
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            failed = []
            try:
                for table, _ in wrong:
                    try:
                        cur.execute(f"ALTER TABLE `{table}` CONVERT TO "
                                    f"CHARACTER SET {CHARSET} "
                                    f"COLLATE {COLLATION}")
                        self.stdout.write(f"  converted {table}")
                    except Exception as exc:  # noqa: BLE001 — report, go on
                        failed.append(table)
                        self.stdout.write(self.style.ERROR(
                            f"  {table}: {exc}"))
            finally:
                cur.execute("SET FOREIGN_KEY_CHECKS = 1")

        if failed:
            raise CommandError(
                f"{len(failed)} table(s) were not converted: "
                f"{', '.join(failed)}. The rest were. Fix the errors above "
                f"and re-run; converted tables are skipped.")
        self.stdout.write(self.style.SUCCESS(
            f"Database and {len(wrong)} table(s) converted to {CHARSET}."))
