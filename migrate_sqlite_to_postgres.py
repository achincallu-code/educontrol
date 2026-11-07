import argparse
import os
import sqlite3
import sys
from pathlib import Path

import app


TABLES = (
    "users",
    "activities",
    "grades",
    "grade_improvements",
    "student_celebrations",
    "notifications",
    "notification_reads",
    "user_preferences",
)


def migrate(source_path):
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("Define DATABASE_URL para seleccionar la base PostgreSQL de destino.")
    if not Path(source_path).is_file():
        raise FileNotFoundError(f"No se encontró la base SQLite: {source_path}")

    app.init_db()
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    try:
        source_tables = {
            row["name"]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "users" not in source_tables:
            raise RuntimeError("La base SQLite no contiene la tabla users.")

        with app.connect() as target:
            target_counts = {
                table: target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in TABLES
            }
            other_users = target.execute(
                "SELECT COUNT(*) FROM users WHERE role<>'teacher' OR username<>'docente'"
            ).fetchone()[0]
            if other_users or any(
                count for table, count in target_counts.items() if table != "users"
            ):
                raise RuntimeError(
                    "La base PostgreSQL ya contiene datos. La migración solo se permite "
                    "a una base nueva para evitar duplicados o sobrescrituras."
                )

            bootstrap_teacher = target.execute(
                "SELECT username,password_hash,role,full_name,approved FROM users "
                "WHERE username='docente'"
            ).fetchone()
            source_has_teacher = source.execute(
                "SELECT 1 FROM users WHERE username='docente' LIMIT 1"
            ).fetchone() is not None
            if bootstrap_teacher:
                target.execute("DELETE FROM users WHERE username='docente'")

            imported = {}
            for table in TABLES:
                if table not in source_tables:
                    imported[table] = 0
                    continue
                source_columns = [
                    row["name"]
                    for row in source.execute(f'PRAGMA table_info("{table}")')
                ]
                target_columns = {
                    row["column_name"]
                    for row in target.execute(
                        """SELECT column_name FROM information_schema.columns
                           WHERE table_schema=current_schema() AND table_name=?""",
                        (table,),
                    ).fetchall()
                }
                columns = [column for column in source_columns if column in target_columns]
                if not columns:
                    imported[table] = 0
                    continue

                quoted_columns = ",".join(f'"{column}"' for column in columns)
                rows = source.execute(
                    f'SELECT {quoted_columns} FROM "{table}"'
                ).fetchall()
                placeholders = ",".join("%s" for _ in columns)
                target.executemany(
                    f"""INSERT INTO "{table}" ({quoted_columns})
                        VALUES ({placeholders}) ON CONFLICT DO NOTHING""",
                    [tuple(row[column] for column in columns) for row in rows],
                )
                imported[table] = len(rows)

            if bootstrap_teacher and not source_has_teacher:
                target.execute(
                    """INSERT INTO users(username,password_hash,role,full_name,approved)
                       VALUES(?,?,?,?,?)""",
                    (
                        bootstrap_teacher["username"],
                        bootstrap_teacher["password_hash"],
                        bootstrap_teacher["role"],
                        bootstrap_teacher["full_name"],
                        bootstrap_teacher["approved"],
                    ),
                )

            for table in TABLES:
                if "id" in {
                    row["column_name"]
                    for row in target.execute(
                        """SELECT column_name FROM information_schema.columns
                           WHERE table_schema=current_schema() AND table_name=?""",
                        (table,),
                    ).fetchall()
                }:
                    target.execute(
                        """SELECT setval(pg_get_serial_sequence(?, 'id'),
                           COALESCE(MAX(id), 1), COUNT(*) > 0) FROM """ + table,
                        (table,),
                    )

            for table, expected in imported.items():
                actual = target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                if actual < expected:
                    raise RuntimeError(
                        f"Verificación incompleta para {table}: se esperaban al menos "
                        f"{expected} registros y hay {actual}."
                    )
    finally:
        source.close()

    return imported


def main():
    parser = argparse.ArgumentParser(
        description="Copia los registros de SQLite a una base PostgreSQL nueva."
    )
    parser.add_argument(
        "source",
        nargs="?",
        default=str(Path(__file__).with_name("educ_control.db")),
        help="Ruta a educ_control.db (por defecto, junto a este script).",
    )
    args = parser.parse_args()
    try:
        imported = migrate(args.source)
    except (OSError, RuntimeError, sqlite3.Error, app.psycopg.Error) as error:
        print(f"Error de migración: {error}", file=sys.stderr)
        return 1
    print("Migración verificada. Registros copiados por tabla:")
    for table, count in imported.items():
        print(f"  {table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
