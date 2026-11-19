import csv, hmac, io, json, os, re, secrets, sqlite3
from datetime import date, timedelta
from functools import wraps
from urllib.parse import parse_qs, urlsplit
from flask import Flask, abort, flash, make_response, redirect, render_template, request, session, url_for
import psycopg
from psycopg import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
secret_key = os.environ.get("SECRET_KEY")
if os.environ.get("RENDER") and not secret_key:
    raise RuntimeError("Configura SECRET_KEY como variable de entorno en Render.")
app.secret_key = secret_key or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "SESSION_COOKIE_SECURE", "true" if os.environ.get("RENDER") else "false"
    ).lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
DB = os.path.join(os.path.dirname(__file__), "educ_control.db")
DEFAULT_PROFILE_AVATAR = "01_CECYTO_pulgar_arriba.png"
INITIAL_PROFILE_AVATAR = "initial"
DEFAULT_USER_PREFERENCES = {
    "theme": "green",
    "appearance": "system",
    "text_size": "normal",
    "reduce_motion": 0,
    "calendar_view": "month",
    "interface_size": "normal",
    "show_cecyto": 1,
    "profile_avatar": DEFAULT_PROFILE_AVATAR,
}
PROFILE_AVATARS = {
    f"{number:02d}": f"{number:02d}_CECYTO_{name}.png"
    for number, name in enumerate(
        (
            "pulgar_arriba", "graduacion", "saludo", "musica",
            "lectura", "futbol", "festejo", "corazon",
            "tecnologia", "relajado", "trofeo", "apoyo",
            "dormilon", "fuerza", "libros", "victoria",
        ),
        start=1,
    )
}
NOTIFICATION_OPTIONS = {
    "activity": "Actividades nuevas",
    "activity_update": "Cambios en actividades",
    "grade": "Calificaciones",
    "completion": "Actividades completadas",
    "recognition": "Reconocimientos",
    "important": "Avisos y fechas importantes",
}
FAVORITE_OPTIONS = {
    "teacher": {
        "dashboard": "Inicio",
        "students": "Alumnos",
        "activities": "Actividades",
        "grades": "Calificaciones",
        "reports": "Reportes",
        "student_calendar": "Calendario",
        "notifications": "Notificaciones",
    },
    "student": {
        "dashboard": "Inicio",
        "activities": "Actividades",
        "grades": "Calificaciones",
        "student_calendar": "Calendario",
        "notifications": "Notificaciones",
        "record": "Mi registro",
        "student_achievements": "Logros",
    },
}
DEFAULT_DASHBOARD_WIDGETS = {
    "teacher": ("teacher_stats", "teacher_quick", "teacher_recent", "teacher_groups", "teacher_grades"),
    "student": (
        "student_stats",
        "student_upcoming",
        "student_progress",
        "student_shortcuts",
        "student_recent",
        "student_calendar",
        "student_achievements",
    ),
}


def _dashboard_widgets_for(role, stored=None, version=1):
    allowed_widgets = DEFAULT_DASHBOARD_WIDGETS.get(role, ())
    if stored is None:
        return list(allowed_widgets)
    widgets = json.loads(stored)
    if not isinstance(widgets, list) or any(
        not isinstance(widget, str) or widget not in allowed_widgets
        for widget in widgets
    ):
        raise ValueError("Las preferencias del panel contienen bloques no válidos.")
    if role == "student" and version < 1:
        widgets.append("student_upcoming")
    return list(dict.fromkeys(widgets))


def _migrate_dashboard_widget_defaults(connection):
    rows = connection.execute(
        """SELECT p.user_id,p.dashboard_widgets,p.dashboard_widgets_version,u.role
           FROM user_preferences p JOIN users u ON u.id=p.user_id
           WHERE p.dashboard_widgets_version<1"""
    ).fetchall()
    for row in rows:
        widgets = _dashboard_widgets_for(
            row["role"], row["dashboard_widgets"], row["dashboard_widgets_version"]
        )
        connection.execute(
            """UPDATE user_preferences
               SET dashboard_widgets=?,dashboard_widgets_version=1 WHERE user_id=?""",
            (json.dumps(widgets) if row["dashboard_widgets"] is not None else None, row["user_id"]),
        )


def _preference_list(stored, allowed_values, description, default_values=None):
    if stored is None:
        return list(allowed_values if default_values is None else default_values)
    values = json.loads(stored)
    if not isinstance(values, list) or any(
        not isinstance(value, str) or value not in allowed_values
        for value in values
    ):
        raise ValueError(f"Las preferencias de {description} contienen opciones no válidas.")
    return list(dict.fromkeys(values))


class HybridRow(dict):
    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _hybrid_row_factory(cursor):
    if cursor.description is None:
        return tuple
    names = [column.name for column in cursor.description]
    return lambda values: HybridRow(names, values)


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def _prepare_sql(sql):
        ignore_insert = re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO\b", sql, re.IGNORECASE)
        if ignore_insert:
            sql = re.sub(
                r"\bINSERT\s+OR\s+IGNORE\s+INTO\b",
                "INSERT INTO",
                sql,
                count=1,
                flags=re.IGNORECASE,
            )
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        return sql.replace("?", "%s")

    def execute(self, sql, parameters=()):
        cursor = self.connection.cursor()
        cursor.execute(self._prepare_sql(sql), parameters)
        return cursor

    def executemany(self, sql, parameters):
        cursor = self.connection.cursor()
        cursor.executemany(self._prepare_sql(sql), parameters)
        return cursor

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return self.connection.__exit__(exc_type, exc_value, traceback)
        finally:
            self.connection.close()


class SQLiteConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        connection_options = {}
        if os.environ.get("RENDER"):
            sslmode = parse_qs(urlsplit(database_url).query).get("sslmode", ["require"])[0]
            if sslmode == "disable":
                raise RuntimeError("La conexión PostgreSQL en Render debe usar SSL.")
            connection_options["sslmode"] = sslmode
        return PostgresConnection(
            psycopg.connect(
                database_url,
                row_factory=_hybrid_row_factory,
                **connection_options,
            )
        )
    if os.environ.get("RENDER"):
        raise RuntimeError("Configura DATABASE_URL para conectar Render con PostgreSQL.")
    c = sqlite3.connect(DB, factory=SQLiteConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def _create_notification(
    connection, *, event_type, title, message, event_date=None,
    target_group=None, target_semester=None, target_student=None, activity_id=None,
    managed=False, created_by=None,
):
    return connection.execute(
        """INSERT INTO notifications
           (type,title,message,event_date,target_group,target_semester,
            target_student,activity_id,managed,created_by)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            event_type, title, message, event_date, target_group, target_semester,
            target_student, activity_id, int(managed), created_by,
        ),
    )


@app.before_request
def protect_post_requests():
    if request.method == "POST":
        submitted = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not submitted or not hmac.compare_digest(expected, submitted):
            abort(400, description="La sesión del formulario expiró. Recarga la página e inténtalo de nuevo.")

def init_db():
    if os.environ.get("DATABASE_URL", "").strip():
        _init_postgres_db()
        return
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('teacher','student')),
          full_name TEXT NOT NULL, group_name TEXT, list_number INTEGER,
          approved INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS activities(
          id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT NOT NULL, subject TEXT NOT NULL,
          semester INTEGER, partial TEXT NOT NULL, title TEXT NOT NULL, description TEXT, due_date TEXT,
          created_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(created_by) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS grades(
          id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL, activity_id INTEGER NOT NULL,
          grade REAL, status TEXT NOT NULL DEFAULT 'Pendiente', comments TEXT, reviewed_at TEXT,
          UNIQUE(student_id,activity_id), FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE,
          FOREIGN KEY(activity_id) REFERENCES activities(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS grade_improvements(
          id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL, activity_id INTEGER NOT NULL,
          previous_grade REAL NOT NULL, grade REAL NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE,
          FOREIGN KEY(activity_id) REFERENCES activities(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS student_celebrations(
          id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL, event_key TEXT NOT NULL,
          title TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          seen_at TEXT, UNIQUE(student_id,event_key),
          FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS notifications(
          id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, title TEXT NOT NULL,
          message TEXT NOT NULL, event_date TEXT, target_group TEXT, target_semester INTEGER,
          target_student INTEGER,
          activity_id INTEGER, managed INTEGER NOT NULL DEFAULT 0, created_by INTEGER,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(target_student) REFERENCES users(id) ON DELETE CASCADE,
          FOREIGN KEY(activity_id) REFERENCES activities(id) ON DELETE CASCADE,
          FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS notification_reads(
          notification_id INTEGER NOT NULL, user_id INTEGER NOT NULL, read_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(notification_id,user_id),
          FOREIGN KEY(notification_id) REFERENCES notifications(id) ON DELETE CASCADE,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS user_preferences(
          user_id INTEGER PRIMARY KEY, theme TEXT NOT NULL DEFAULT 'green',
          appearance TEXT NOT NULL DEFAULT 'system', text_size TEXT NOT NULL DEFAULT 'normal',
          reduce_motion INTEGER NOT NULL DEFAULT 0, dashboard_widgets TEXT,
          notification_types TEXT, favorite_links TEXT,
          calendar_view TEXT NOT NULL DEFAULT 'month',
          interface_size TEXT NOT NULL DEFAULT 'normal',
          show_cecyto INTEGER NOT NULL DEFAULT 1,
          dashboard_widgets_version INTEGER NOT NULL DEFAULT 0,
          profile_avatar TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        """)
        existing_columns = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
        for column, definition in (
            ("first_name", "TEXT"),
            ("last_name", "TEXT"),
            ("email", "TEXT"),
            ("semester", "INTEGER"),
            ("active", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if column not in existing_columns:
                c.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        c.execute("UPDATE users SET first_name=full_name WHERE first_name IS NULL AND role='student'")
        c.execute("UPDATE users SET email=NULL WHERE email=''")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique "
            "ON users(email) WHERE email IS NOT NULL"
        )
        activity_columns = {row["name"] for row in c.execute("PRAGMA table_info(activities)")}
        if "semester" not in activity_columns:
            c.execute("ALTER TABLE activities ADD COLUMN semester INTEGER")
            c.execute(
                """UPDATE activities AS a SET semester=(
                     SELECT MIN(u.semester) FROM users u
                     WHERE u.role='student' AND u.approved=1
                       AND u.group_name=a.group_name AND u.semester IS NOT NULL
                     HAVING COUNT(DISTINCT u.semester)=1
                   )"""
            )
        notification_columns = {
            row["name"] for row in c.execute("PRAGMA table_info(notifications)")
        }
        if "target_semester" not in notification_columns:
            c.execute("ALTER TABLE notifications ADD COLUMN target_semester INTEGER")
            c.execute(
                """UPDATE notifications SET target_semester=(
                     SELECT a.semester FROM activities a
                     WHERE a.id=notifications.activity_id
                   ) WHERE activity_id IS NOT NULL"""
            )
            c.execute(
                """UPDATE notifications SET target_semester=(
                     SELECT MIN(u.semester) FROM users u
                     WHERE u.role='student' AND u.approved=1
                       AND u.group_name=notifications.target_group
                       AND u.semester IS NOT NULL
                     HAVING COUNT(DISTINCT u.semester)=1
                   ) WHERE target_group IS NOT NULL AND activity_id IS NULL"""
            )
        grade_columns = {row["name"] for row in c.execute("PRAGMA table_info(grades)")}
        if "submitted_at" not in grade_columns:
            c.execute("ALTER TABLE grades ADD COLUMN submitted_at TEXT")
        preference_columns = {row["name"] for row in c.execute("PRAGMA table_info(user_preferences)")}
        for column, definition in (
            ("dashboard_widgets", "TEXT"),
            ("notification_types", "TEXT"),
            ("favorite_links", "TEXT"),
            ("calendar_view", "TEXT NOT NULL DEFAULT 'month'"),
            ("interface_size", "TEXT NOT NULL DEFAULT 'normal'"),
            ("show_cecyto", "INTEGER NOT NULL DEFAULT 1"),
            ("dashboard_widgets_version", "INTEGER NOT NULL DEFAULT 0"),
            ("profile_avatar", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in preference_columns:
                c.execute(f"ALTER TABLE user_preferences ADD COLUMN {column} {definition}")
        if not c.execute("SELECT id FROM users WHERE username='docente'").fetchone():
            initial_teacher_password = os.environ.get("INITIAL_TEACHER_PASSWORD")
            if not initial_teacher_password:
                raise RuntimeError(
                    "Configura INITIAL_TEACHER_PASSWORD antes de crear la cuenta docente."
                )
            if len(initial_teacher_password) < 12:
                raise RuntimeError("INITIAL_TEACHER_PASSWORD debe tener al menos 12 caracteres.")
            c.execute("INSERT INTO users(username,password_hash,role,full_name,approved) VALUES(?,?,?,?,1)",
                      ("docente", generate_password_hash(initial_teacher_password), "teacher", "Docente CECyTEM"))
        _migrate_dashboard_widget_defaults(c)


def _init_postgres_db():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('teacher','student')),
          full_name TEXT NOT NULL, group_name TEXT, list_number INTEGER,
          approved INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          first_name TEXT, last_name TEXT, email TEXT, semester INTEGER,
          active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS activities(
          id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          group_name TEXT NOT NULL, semester INTEGER, subject TEXT NOT NULL, partial TEXT NOT NULL,
          title TEXT NOT NULL, description TEXT, due_date TEXT, created_by BIGINT,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(created_by) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS grades(
          id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          student_id BIGINT NOT NULL, activity_id BIGINT NOT NULL, grade DOUBLE PRECISION,
          status TEXT NOT NULL DEFAULT 'Pendiente', comments TEXT, reviewed_at TIMESTAMP,
          submitted_at TEXT, UNIQUE(student_id,activity_id),
          FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE,
          FOREIGN KEY(activity_id) REFERENCES activities(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS grade_improvements(
          id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          student_id BIGINT NOT NULL, activity_id BIGINT NOT NULL,
          previous_grade DOUBLE PRECISION NOT NULL, grade DOUBLE PRECISION NOT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE,
          FOREIGN KEY(activity_id) REFERENCES activities(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS student_celebrations(
          id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          student_id BIGINT NOT NULL, event_key TEXT NOT NULL, title TEXT NOT NULL,
          message TEXT NOT NULL, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          seen_at TIMESTAMP, UNIQUE(student_id,event_key),
          FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS notifications(
          id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          type TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL,
          event_date TEXT, target_group TEXT, target_semester INTEGER,
          target_student BIGINT, activity_id BIGINT,
          managed INTEGER NOT NULL DEFAULT 0, created_by BIGINT,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(target_student) REFERENCES users(id) ON DELETE CASCADE,
          FOREIGN KEY(activity_id) REFERENCES activities(id) ON DELETE CASCADE,
          FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS notification_reads(
          notification_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
          read_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(notification_id,user_id),
          FOREIGN KEY(notification_id) REFERENCES notifications(id) ON DELETE CASCADE,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS user_preferences(
          user_id BIGINT PRIMARY KEY, theme TEXT NOT NULL DEFAULT 'green',
          appearance TEXT NOT NULL DEFAULT 'system', text_size TEXT NOT NULL DEFAULT 'normal',
          reduce_motion INTEGER NOT NULL DEFAULT 0, dashboard_widgets TEXT,
          notification_types TEXT, favorite_links TEXT,
          calendar_view TEXT NOT NULL DEFAULT 'month',
          interface_size TEXT NOT NULL DEFAULT 'normal',
          show_cecyto INTEGER NOT NULL DEFAULT 1,
          dashboard_widgets_version INTEGER NOT NULL DEFAULT 0,
          profile_avatar TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique
          ON users(email) WHERE email IS NOT NULL;
        """)
        for column, definition in (
            ("first_name", "TEXT"),
            ("last_name", "TEXT"),
            ("email", "TEXT"),
            ("semester", "INTEGER"),
            ("active", "INTEGER NOT NULL DEFAULT 1"),
        ):
            c.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {column} {definition}")
        c.execute(
            "ALTER TABLE grades ADD COLUMN IF NOT EXISTS submitted_at TEXT"
        )
        activity_columns = {
            row["column_name"] for row in c.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema=current_schema() AND table_name='activities'"""
            ).fetchall()
        }
        if "semester" not in activity_columns:
            c.execute("ALTER TABLE activities ADD COLUMN semester INTEGER")
            c.execute(
                """UPDATE activities AS a SET semester=(
                     SELECT MIN(u.semester) FROM users u
                     WHERE u.role='student' AND u.approved=1
                       AND u.group_name=a.group_name AND u.semester IS NOT NULL
                     HAVING COUNT(DISTINCT u.semester)=1
                   )"""
            )
        notification_columns = {
            row["column_name"] for row in c.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema=current_schema() AND table_name='notifications'"""
            ).fetchall()
        }
        if "target_semester" not in notification_columns:
            c.execute("ALTER TABLE notifications ADD COLUMN target_semester INTEGER")
            c.execute(
                """UPDATE notifications AS n SET target_semester=(
                     SELECT a.semester FROM activities a WHERE a.id=n.activity_id
                   ) WHERE n.activity_id IS NOT NULL"""
            )
            c.execute(
                """UPDATE notifications AS n SET target_semester=(
                     SELECT MIN(u.semester) FROM users u
                     WHERE u.role='student' AND u.approved=1
                       AND u.group_name=n.target_group AND u.semester IS NOT NULL
                     HAVING COUNT(DISTINCT u.semester)=1
                   ) WHERE n.target_group IS NOT NULL AND n.activity_id IS NULL"""
            )
        for column, definition in (
            ("dashboard_widgets", "TEXT"),
            ("notification_types", "TEXT"),
            ("favorite_links", "TEXT"),
            ("calendar_view", "TEXT NOT NULL DEFAULT 'month'"),
            ("interface_size", "TEXT NOT NULL DEFAULT 'normal'"),
            ("show_cecyto", "INTEGER NOT NULL DEFAULT 1"),
            ("dashboard_widgets_version", "INTEGER NOT NULL DEFAULT 0"),
            ("profile_avatar", "TEXT NOT NULL DEFAULT ''"),
        ):
            c.execute(
                f"ALTER TABLE user_preferences ADD COLUMN IF NOT EXISTS {column} {definition}"
            )
        c.execute("UPDATE users SET first_name=full_name WHERE first_name IS NULL AND role='student'")
        c.execute("UPDATE users SET email=NULL WHERE email=''")
        if not c.execute("SELECT id FROM users WHERE username='docente'").fetchone():
            initial_teacher_password = os.environ.get("INITIAL_TEACHER_PASSWORD")
            if not initial_teacher_password or len(initial_teacher_password) < 12:
                raise RuntimeError(
                    "Configura INITIAL_TEACHER_PASSWORD con al menos 12 caracteres."
                )
            c.execute(
                """INSERT INTO users(username,password_hash,role,full_name,approved)
                   VALUES(?,?,?,?,1)""",
                (
                    "docente",
                    generate_password_hash(initial_teacher_password),
                    "teacher",
                    "Docente CECyTEM",
                ),
            )
        _migrate_dashboard_widget_defaults(c)


def require(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapped(*a, **kw):
            user_id = session.get("user_id")
            if not user_id:
                return redirect(url_for("login"))
            with connect() as c:
                user = c.execute(
                    """SELECT role,full_name,approved,active,group_name,semester
                       FROM users WHERE id=?""",
                    (user_id,),
                ).fetchone()
            if not user or not user["active"] or (user["role"] == "student" and not user["approved"]):
                session.clear()
                flash("Tu cuenta no está activa. Contacta al docente administrador.", "error")
                return redirect(url_for("login"))
            session["role"] = user["role"]
            session["full_name"] = user["full_name"]
            session["group_name"] = user["group_name"]
            session["semester"] = user["semester"]
            if role and user["role"] != role:
                abort(403)
            return fn(*a, **kw)
        return wrapped
    return deco

@app.context_processor
def shared():
    unread_notifications = 0
    user_preferences = dict(DEFAULT_USER_PREFERENCES)
    dashboard_widgets = list(DEFAULT_DASHBOARD_WIDGETS.get(session.get("role"), ()))
    favorite_links = []
    notification_types = list(NOTIFICATION_OPTIONS)
    user_id = session.get("user_id")
    role = session.get("role")
    if user_id and role:
        with connect() as c:
            preference_row = c.execute(
                """SELECT theme,appearance,text_size,reduce_motion,dashboard_widgets,
                          dashboard_widgets_version,
                          notification_types,favorite_links,calendar_view,interface_size,show_cecyto,
                          profile_avatar
                   FROM user_preferences WHERE user_id=?""",
                (user_id,),
            ).fetchone()
            if preference_row:
                stored_preferences = dict(preference_row)
                user_preferences.update({
                    key: value for key, value in stored_preferences.items()
                    if key not in {
                        "dashboard_widgets", "notification_types", "favorite_links"
                    }
                })
                dashboard_widgets = _dashboard_widgets_for(
                    role,
                    stored_preferences["dashboard_widgets"],
                    stored_preferences["dashboard_widgets_version"],
                )
                notification_types = _preference_list(
                    stored_preferences["notification_types"],
                    NOTIFICATION_OPTIONS,
                    "notificaciones",
                )
                favorite_links = _preference_list(
                    stored_preferences["favorite_links"],
                    FAVORITE_OPTIONS.get(role, {}),
                    "accesos favoritos",
                    default_values=(),
                )
            user_preferences["profile_avatar"] = (
                user_preferences["profile_avatar"] or DEFAULT_PROFILE_AVATAR
            )
            scope, parameters = _notification_scope(
                role, user_id, session.get("group_name"), session.get("semester")
            )
            if notification_types:
                placeholders = ",".join("?" for _ in notification_types)
                unread_notifications = c.execute(
                    f"""SELECT COUNT(*) AS total FROM notifications n
                        LEFT JOIN notification_reads nr
                          ON nr.notification_id=n.id AND nr.user_id=?
                        WHERE nr.read_at IS NULL AND {scope}
                          AND n.type IN ({placeholders})""",
                    (user_id, *parameters, *notification_types),
                ).fetchone()["total"]
    return {
        "role": role,
        "current_user": session.get("full_name"),
        "unread_notifications": unread_notifications,
        "user_preferences": user_preferences,
        "dashboard_widgets": dashboard_widgets,
        "notification_types": notification_types,
        "notification_options": NOTIFICATION_OPTIONS,
        "favorite_links": favorite_links,
        "favorite_options": FAVORITE_OPTIONS.get(role, {}),
        "initial_profile_avatar": INITIAL_PROFILE_AVATAR,
        "csrf_token": csrf_token,
    }

@app.route("/")
def home(): return redirect(url_for("dashboard" if session.get("user_id") else "login"))


@app.get("/healthz")
def health_check():
    with connect() as c:
        c.execute("SELECT 1")
    return {"status": "ok"}


@app.route("/registro", methods=["GET","POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        group = request.form.get("group_name", "").strip().upper()
        list_number = request.form.get("list_number", "").strip()
        semester = request.form.get("semester", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        name = f"{first_name} {last_name}".strip()
        username = email
        if not all([first_name, last_name, group, list_number, semester, email, password]):
            flash("Completa todos los campos.", "error")
        elif len(first_name) > 80 or len(last_name) > 100 or len(group) > 20:
            flash("Revisa la longitud del nombre, apellidos o grupo.", "error")
        elif not re.fullmatch(
            r"[^@\s]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}",
            email,
        ) or len(email) > 254:
            flash("Escribe un correo electrónico válido.", "error")
        elif not list_number.isdigit() or len(list_number) > 4 or int(list_number) < 1:
            flash("El número de lista debe ser un entero positivo de hasta 4 cifras.", "error")
        elif semester not in {"1", "2", "3", "4", "5", "6"}:
            flash("El semestre debe estar entre 1 y 6.", "error")
        elif len(password) < 8 or len(password) > 128:
            flash("La contraseña debe tener entre 8 y 128 caracteres.", "error")
        else:
            try:
                with connect() as c:
                    if c.execute(
                        "SELECT id FROM users WHERE lower(username)=? OR lower(email)=?",
                        (username, email),
                    ).fetchone():
                        flash("Ese correo ya tiene una cuenta registrada.", "error")
                    elif c.execute(
                        """SELECT id FROM users WHERE role='student'
                           AND group_name=? AND semester=? AND list_number=?""",
                        (group, int(semester), int(list_number)),
                    ).fetchone():
                        flash("Ese número de lista ya está registrado en ese grupo y semestre.", "error")
                    else:
                        c.execute(
                            """INSERT INTO users
                               (username,password_hash,role,full_name,first_name,last_name,email,
                                group_name,list_number,semester,approved)
                               VALUES(?,?,'student',?,?,?,?,?,?,?,0)""",
                            (
                                username, generate_password_hash(password), name, first_name,
                                last_name, email, group, int(list_number), int(semester),
                            ),
                        )
                        session.pop("csrf_token", None)
                        flash("¡Registro recibido! El docente debe aprobar tu cuenta antes de iniciar sesión.", "success")
                        return redirect(url_for("login"))
            except (sqlite3.IntegrityError, IntegrityError):
                flash("El correo o número de lista ya está registrado.", "error")
    return render_template("register.html")

@app.route("/login",methods=["GET","POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip().lower()
        password = request.form.get("password", "")
        remember = request.form.get("remember") == "on"
        with connect() as c:
            user = c.execute(
                "SELECT * FROM users WHERE lower(username)=? OR lower(email)=? LIMIT 1",
                (identifier, identifier),
            ).fetchone()
        if user and check_password_hash(user["password_hash"],password):
            if not user["active"]:
                flash("Tu cuenta está desactivada. Contacta al docente administrador.", "error")
                return redirect(url_for("login"))
            if user["role"]=="student" and not user["approved"]:
                flash("Tu cuenta está pendiente de aprobación.","error")
                return redirect(url_for("login"))
            session.clear()
            session.update(
                user_id=user["id"],
                role=user["role"],
                full_name=user["full_name"],
                group_name=user["group_name"],
                semester=user["semester"],
                student_welcome=user["role"] == "student",
                _permanent=remember,
            )
            return redirect(url_for("dashboard"))
        flash("Usuario o contraseña incorrectos.","error")
    return render_template("login.html")


@app.post("/salir")
def logout():
    session.clear()
    flash("Sesión cerrada.","success")
    return redirect(url_for("login"))

@app.route("/dashboard")
@require()
def dashboard():
    uid=session["user_id"]
    with connect() as c:
        if session["role"]=="teacher":
            stats = {
                "pending": c.execute(
                    "SELECT COUNT(*) n FROM users WHERE role='student' AND approved=0 AND active=1"
                ).fetchone()["n"],
            }
            return render_template("dashboard.html", stats=stats)
        else:
            (
                student, total, completed, reviewed, average, achievements, pending_count,
                _, _,
            ) = _student_learning_data(uid)
            stats = {
                "group": student["group_name"],
                "semester": student["semester"],
                "list_number": student["list_number"],
                "total": total,
                "done": completed,
                "completed": completed,
                "pending": pending_count,
                "reviewed": reviewed,
                "progress": round(completed / total * 100) if total else 0,
                "average": average if average is not None else "—",
            }
            with connect() as student_db:
                recent = student_db.execute(
                    """SELECT a.*,g.status,g.grade FROM activities a
                       LEFT JOIN grades g ON g.activity_id=a.id AND g.student_id=?
                       WHERE (a.group_name=? AND a.semester=?) OR g.student_id=?
                       ORDER BY COALESCE(a.due_date,'9999-12-31'),a.id DESC LIMIT 5""",
                    (uid, student["group_name"], student["semester"], uid),
                ).fetchall()
                upcoming_due = student_db.execute(
                    """SELECT a.id,a.title,a.subject,a.due_date
                       FROM activities a LEFT JOIN grades g
                         ON g.activity_id=a.id AND g.student_id=?
                       WHERE ((a.group_name=? AND a.semester=?) OR g.student_id=?)
                         AND a.due_date>=?
                         AND COALESCE(g.status,'Pendiente')='Pendiente'
                       ORDER BY a.due_date,LOWER(a.title),a.id LIMIT 4""",
                    (uid, student["group_name"], student["semester"], uid, date.today().isoformat()),
                ).fetchall()
                formatted_upcoming = []
                for activity_row in upcoming_due:
                    activity = dict(activity_row)
                    due_date = date.fromisoformat(activity["due_date"])
                    days_until = (due_date - date.today()).days
                    activity["date_display"] = due_date.strftime("%d/%m/%Y")
                    activity["days_until"] = days_until
                    activity["due_label"] = (
                        "Hoy" if days_until == 0
                        else "Mañana" if days_until == 1
                        else f"En {days_until} días"
                    )
                    formatted_upcoming.append(activity)
                upcoming_due = formatted_upcoming
                preference_row = student_db.execute(
                    "SELECT notification_types FROM user_preferences WHERE user_id=?",
                    (uid,),
                ).fetchone()
                notification_types = _preference_list(
                    preference_row["notification_types"] if preference_row else None,
                    NOTIFICATION_OPTIONS,
                    "notificaciones",
                )
                recent_notifications = []
                if notification_types:
                    type_placeholders = ",".join("?" for _ in notification_types)
                    recent_notifications = student_db.execute(
                        f"""SELECT n.id,n.type,n.title,n.message,n.event_date,n.created_at,
                                   nr.read_at IS NOT NULL AS is_read
                            FROM notifications n
                            LEFT JOIN notification_reads nr
                              ON nr.notification_id=n.id AND nr.user_id=?
                            WHERE (n.target_student=? OR
                                   (n.target_student IS NULL AND
                                    (n.target_group IS NULL OR
                                     (n.target_group=? AND n.target_semester=?))))
                              AND n.type IN ({type_placeholders})
                            ORDER BY n.created_at DESC,n.id DESC LIMIT 4""",
                        (uid, uid, student["group_name"], student["semester"], *notification_types),
                    ).fetchall()
                _sync_student_celebrations(student_db, uid, achievements)
                new_celebrations = student_db.execute(
                    """SELECT id,title,message FROM student_celebrations
                       WHERE student_id=? AND seen_at IS NULL
                       ORDER BY id LIMIT 12""",
                    (uid,),
                ).fetchall()
            student_welcome = bool(session.pop("student_welcome", False))
            return render_template(
                "dashboard.html", stats=stats, recent=recent,
                student=student, achievements=achievements,
                upcoming_due=upcoming_due,
                recent_notifications=recent_notifications,
                new_celebrations=new_celebrations,
                student_welcome=student_welcome,
            )
    return render_template("dashboard.html",stats=stats,recent=recent)


def _student_achievements(*, total, completed, reviewed, average, on_time, improved):
    return [
        {"key": "first-step", "icon": "🌱", "title": "Primer paso",
         "description": "Completa tu primera actividad.", "earned": completed >= 1},
        {"key": "five-activities", "icon": "📚", "title": "Constancia",
         "description": "Completa cinco actividades.", "earned": completed >= 5},
        {"key": "first-review", "icon": "✅", "title": "Trabajo revisado",
         "description": "Obtén tu primera actividad revisada.", "earned": reviewed >= 1},
        {"key": "excellent-grade", "icon": "⭐", "title": "Excelencia",
         "description": "Alcanza un promedio de 9 o más.",
         "earned": average is not None and average >= 9},
        {"key": "on-time", "icon": "⏱️", "title": "A tiempo",
         "description": "Entrega una actividad a más tardar en su fecha límite.",
         "earned": on_time >= 1},
        {"key": "improved-grade", "icon": "📈", "title": "Mejora constante",
         "description": "Mejora una calificación respecto a una evaluación anterior.",
         "earned": improved},
        {"key": "all-complete", "icon": "🏆", "title": "Meta cumplida",
         "description": "Completa todas tus actividades asignadas.",
         "earned": total >= 3 and completed == total},
    ]


def _sync_student_celebrations(connection, user_id, achievements):
    student = connection.execute(
        "SELECT full_name FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    student_name = student["full_name"] if student else "Un alumno"
    for achievement in achievements:
        if achievement["earned"]:
            inserted = connection.execute(
                """INSERT OR IGNORE INTO student_celebrations
                   (student_id,event_key,title,message)
                   VALUES(?,?,?,?)""",
                (
                    user_id,
                    f"achievement:{achievement['key']}",
                    f"¡Nuevo logro: {achievement['title']}!",
                    achievement["description"],
                ),
            )
            if inserted.rowcount:
                _create_notification(
                    connection,
                    event_type="recognition",
                    title=f"¡Nuevo reconocimiento: {achievement['title']}!",
                    message=f"{student_name}: {achievement['description']}",
                    event_date=date.today().isoformat(),
                    target_student=user_id,
                )


def _student_learning_data(user_id):
    with connect() as c:
        student = c.execute(
            """SELECT id,full_name,email,group_name,semester,list_number,active
               FROM users WHERE id=? AND role='student' AND approved=1 AND active=1""",
            (user_id,),
        ).fetchone()
        if not student:
            abort(404)
        grade_rows = c.execute(
            """SELECT g.status,g.grade,g.submitted_at,a.due_date FROM activities a
               LEFT JOIN grades g ON g.activity_id=a.id AND g.student_id=?
               WHERE (a.group_name=? AND a.semester=?) OR g.student_id=?""",
            (user_id, student["group_name"], student["semester"], user_id),
        ).fetchall()
        improved = c.execute(
            "SELECT EXISTS(SELECT 1 FROM grade_improvements WHERE student_id=?)",
            (user_id,),
        ).fetchone()[0]
    total = len(grade_rows)
    completed = sum(row["status"] in ("Entregada", "Revisada") for row in grade_rows)
    reviewed = sum(row["status"] == "Revisada" for row in grade_rows)
    pending_count = sum(row["status"] not in ("Entregada", "Revisada") for row in grade_rows)
    on_time = sum(
        row["status"] in ("Entregada", "Revisada")
        and row["submitted_at"] is not None
        and row["due_date"] is not None
        and row["submitted_at"] <= row["due_date"]
        for row in grade_rows
    )
    graded_values = [row["grade"] for row in grade_rows if row["grade"] is not None]
    average = round(sum(graded_values) / len(graded_values), 1) if graded_values else None
    achievements = _student_achievements(
        total=total, completed=completed, reviewed=reviewed, average=average,
        on_time=on_time, improved=bool(improved),
    )
    return (
        student, total, completed, reviewed, average, achievements, pending_count,
        on_time, bool(improved),
    )


@app.route("/calendario")
@require()
def student_calendar():
    student = None
    group_name = None
    selected_group = request.args.get("group", "").strip().upper()
    selected_semester = request.args.get("semester", "").strip()
    if selected_semester not in {"1", "2", "3", "4", "5", "6"}:
        selected_semester = ""
    incomplete_cohort_filter = bool(selected_group) != bool(selected_semester)
    with connect() as c:
        if session["role"] == "student":
            student, _, _, _, _, _, _, _, _ = _student_learning_data(session["user_id"])
            group_name = student["group_name"]
            session["group_name"] = group_name
            session["semester"] = student["semester"]
            rows = c.execute(
                """SELECT a.id,a.title,a.subject,a.due_date,a.group_name,a.semester,
                          COALESCE(g.status,'Pendiente') AS status
                   FROM activities a LEFT JOIN grades g
                     ON g.activity_id=a.id AND g.student_id=?
                   WHERE ((a.group_name=? AND a.semester=?) OR g.student_id=?)
                     AND a.due_date IS NOT NULL
                   ORDER BY a.due_date,LOWER(a.title)""",
                (session["user_id"], group_name, student["semester"], session["user_id"]),
            ).fetchall()
        else:
            conditions = ["a.due_date IS NOT NULL"]
            parameters = []
            if incomplete_cohort_filter:
                conditions.append("1=0")
            elif selected_group and selected_semester:
                conditions.append("a.group_name=?")
                parameters.append(selected_group)
                conditions.append("a.semester=?")
                parameters.append(int(selected_semester))
            rows = c.execute(
                """SELECT a.id,a.title,a.subject,a.due_date,a.group_name,a.semester,
                          CASE WHEN a.semester IS NULL THEN 'Pendiente'
                               WHEN SUM(CASE WHEN u.group_name=a.group_name
                                                  AND u.semester=a.semester
                                                  AND g.status IN ('Pendiente','Entregada')
                                             THEN 1 ELSE 0 END)>0
                               THEN 'Pendiente' ELSE 'Revisada' END AS status
                   FROM activities a LEFT JOIN grades g ON g.activity_id=a.id
                   LEFT JOIN users u ON u.id=g.student_id
                   WHERE """ + " AND ".join(conditions) + """
                   GROUP BY a.id ORDER BY a.due_date,LOWER(a.title)""",
                parameters,
            ).fetchall()
        events = []
        today = date.today().isoformat()
        for row in rows:
            completed = row["status"] in ("Entregada", "Revisada")
            events.append({
                "id": row["id"],
                "date": row["due_date"],
                "title": row["title"],
                "detail": (
                    f"{row['subject']} · Grupo {row['group_name']} · "
                    f"Semestre {row['semester'] or 'sin asignar'}"
                ),
                "type": "activity",
                "state": "completed" if completed else "overdue" if row["due_date"] < today else "upcoming",
                "label": "Completada" if completed else "Vencida" if row["due_date"] < today else "Próxima",
            })
        if session["role"] == "teacher":
            notice_conditions = ["type='important'", "event_date IS NOT NULL"]
            notice_parameters = []
            if incomplete_cohort_filter:
                notice_conditions.append("1=0")
            elif selected_group and selected_semester:
                notice_conditions.append(
                    "((target_group IS NULL AND target_semester IS NULL) OR "
                    "(target_group=? AND target_semester=?))"
                )
                notice_parameters.extend((selected_group, int(selected_semester)))
            notices = c.execute(
                """SELECT id,event_date,title,message,target_group,target_semester
                   FROM notifications n WHERE """
                + " AND ".join(notice_conditions)
                + " ORDER BY event_date,LOWER(title)",
                notice_parameters,
            ).fetchall()
        else:
            notice_scope = (
                "target_group IS NULL"
                if student["semester"] is None
                else "(target_group IS NULL OR (target_group=? AND target_semester=?))"
            )
            notice_parameters = (
                ()
                if student["semester"] is None
                else (group_name, student["semester"])
            )
            notices = c.execute(
                """SELECT id,event_date,title,message,target_group,target_semester
                   FROM notifications n
                   WHERE type='important' AND event_date IS NOT NULL
                     AND """ + notice_scope + """
                   ORDER BY event_date,LOWER(title)""",
                notice_parameters,
            ).fetchall()
        for notice in notices:
            notice_detail = notice["message"]
            if session["role"] == "teacher" and notice["target_group"]:
                notice_detail += (
                    f" · Grupo {notice['target_group']} · "
                    f"Semestre {notice['target_semester'] or 'sin asignar'}"
                )
            events.append({
                "id": notice["id"],
                "date": notice["event_date"],
                "title": notice["title"],
                "detail": notice_detail,
                "type": "important",
                "state": "past" if notice["event_date"] < today else "upcoming",
                "label": "Fecha importante",
            })
        events.sort(key=lambda event: (event["date"], event["title"].casefold()))
        visible_events = [
            event for event in events
            if event["date"] >= today or event["state"] in ("overdue", "past")
        ]
    with connect() as c:
        groups = c.execute(
            """SELECT group_name FROM users
               WHERE role='student' AND approved=1 AND active=1
               GROUP BY group_name
               ORDER BY LOWER(group_name)"""
        ).fetchall() if session["role"] == "teacher" else []
        semester_options = c.execute(
            """SELECT DISTINCT semester FROM users
               WHERE role='student' AND approved=1 AND active=1 AND semester IS NOT NULL
               ORDER BY semester"""
        ).fetchall() if session["role"] == "teacher" else []
    upcoming_events = [event for event in events if event["date"] >= today][:4]
    return render_template(
        "student_calendar.html",
        student=student,
        events=visible_events,
        calendar_events=visible_events,
        upcoming_events=upcoming_events,
        groups=groups,
        semester_options=semester_options,
        selected_group=selected_group,
        selected_semester=selected_semester,
        incomplete_cohort_filter=incomplete_cohort_filter,
        is_teacher=session["role"] == "teacher",
    )


def _notification_scope(role, user_id, group_name, semester=None):
    if role == "teacher":
        return "1=1", ()
    if group_name is None or semester is None:
        return (
            "(n.target_student=? OR (n.target_student IS NULL AND n.target_group IS NULL))",
            (user_id,),
        )
    return (
        "(n.target_student=? OR (n.target_student IS NULL "
        "AND (n.target_group IS NULL OR "
        "(n.target_group=? AND n.target_semester=?))))",
        (user_id, group_name, semester),
    )


def _user_notification_types(connection, user_id):
    row = connection.execute(
        "SELECT notification_types FROM user_preferences WHERE user_id=?",
        (user_id,),
    ).fetchone()
    return _preference_list(
        row["notification_types"] if row else None,
        NOTIFICATION_OPTIONS,
        "notificaciones",
    )


@app.route("/notificaciones", methods=["GET", "POST"])
@require()
def notifications():
    if request.method == "POST":
        if session["role"] != "teacher":
            abort(403)
        title = request.form.get("title", "").strip()
        message = request.form.get("message", "").strip()
        event_date = request.form.get("event_date", "").strip()
        target_group = request.form.get("group_name", "").strip().upper()
        target_semester_raw = request.form.get("semester", "").strip()
        target_semester = (
            int(target_semester_raw)
            if target_semester_raw in {"1", "2", "3", "4", "5", "6"}
            else None
        )
        if not title or not message or not event_date:
            flash("Completa el título, el mensaje y la fecha importante.", "error")
        elif bool(target_group) != bool(target_semester_raw) or (
            target_semester_raw and target_semester is None
        ):
            flash("Selecciona grupo y semestre, o deja ambos en 'Todos los grupos'.", "error")
        elif len(title) > 160 or len(message) > 1000:
            flash("El título admite hasta 160 caracteres y el mensaje hasta 1,000.", "error")
        elif not _valid_iso_date(event_date):
            flash("Selecciona una fecha válida.", "error")
        else:
            with connect() as c:
                if target_group and not c.execute(
                    """SELECT 1 FROM users WHERE role='student' AND approved=1 AND active=1
                       AND group_name=? AND semester=? LIMIT 1""",
                    (target_group, target_semester),
                ).fetchone():
                    flash("No hay alumnos activos aprobados en ese grupo y semestre.", "error")
                    return redirect(url_for("notifications"))
                _create_notification(
                    c,
                    event_type="important",
                    title=title,
                    message=message,
                    event_date=event_date,
                    target_group=target_group or None,
                    target_semester=target_semester,
                    managed=True,
                    created_by=session["user_id"],
                )
            flash("Fecha importante publicada en el calendario y las notificaciones.", "success")
            return redirect(url_for("notifications"))
    user_id = session["user_id"]
    role = session["role"]
    group_name = session.get("group_name")
    scope, parameters = _notification_scope(
        role, user_id, group_name, session.get("semester")
    )
    with connect() as c:
        selected_types = _user_notification_types(c, user_id)
        if selected_types:
            type_filter = " AND n.type IN (" + ",".join("?" for _ in selected_types) + ")"
        else:
            type_filter = " AND 1=0"
        rows = c.execute(
            f"""SELECT n.*,nr.read_at,target_user.full_name AS target_name
                FROM notifications n
                LEFT JOIN users target_user ON target_user.id=n.target_student
                LEFT JOIN notification_reads nr
                  ON nr.notification_id=n.id AND nr.user_id=?
                WHERE {scope}{type_filter}
                ORDER BY CASE WHEN nr.read_at IS NULL THEN 0 ELSE 1 END,
                         n.created_at DESC,n.id DESC LIMIT 200""",
            (user_id, *parameters, *selected_types),
        ).fetchall()
        groups = c.execute(
            """SELECT group_name FROM users
               WHERE role='student' AND approved=1 AND active=1
               GROUP BY group_name
               ORDER BY LOWER(group_name)"""
        ).fetchall() if role == "teacher" else []
        semester_options = c.execute(
            """SELECT DISTINCT semester FROM users
               WHERE role='student' AND approved=1 AND active=1 AND semester IS NOT NULL
               ORDER BY semester"""
        ).fetchall() if role == "teacher" else []
    return render_template(
        "notifications.html",
        notifications=rows,
        groups=groups,
        semester_options=semester_options,
        is_teacher=role == "teacher",
    )


@app.post("/notificaciones/<int:notification_id>/leer")
@require()
def mark_notification_read(notification_id):
    scope, parameters = _notification_scope(
        session["role"], session["user_id"], session.get("group_name"),
        session.get("semester"),
    )
    with connect() as c:
        selected_types = _user_notification_types(c, session["user_id"])
        type_filter = (
            " AND n.type IN (" + ",".join("?" for _ in selected_types) + ")"
            if selected_types else " AND 1=0"
        )
        visible = c.execute(
            f"SELECT n.id FROM notifications n WHERE n.id=? AND {scope}{type_filter}",
            (notification_id, *parameters, *selected_types),
        ).fetchone()
        if not visible:
            abort(404)
        c.execute(
            "INSERT OR IGNORE INTO notification_reads(notification_id,user_id) VALUES(?,?)",
            (notification_id, session["user_id"]),
        )
    return redirect(url_for("notifications"))


@app.post("/notificaciones/leer-todas")
@require()
def mark_all_notifications_read():
    scope, parameters = _notification_scope(
        session["role"], session["user_id"], session.get("group_name"),
        session.get("semester"),
    )
    with connect() as c:
        selected_types = _user_notification_types(c, session["user_id"])
        type_filter = (
            " AND n.type IN (" + ",".join("?" for _ in selected_types) + ")"
            if selected_types else " AND 1=0"
        )
        c.execute(
            f"""INSERT OR IGNORE INTO notification_reads(notification_id,user_id)
                SELECT n.id,? FROM notifications n WHERE {scope}{type_filter}""",
            (session["user_id"], *parameters, *selected_types),
        )
    flash("Notificaciones marcadas como leídas.", "success")
    return redirect(url_for("notifications"))


@app.post("/notificaciones/<int:notification_id>/eliminar")
@require("teacher")
def delete_managed_notification(notification_id):
    with connect() as c:
        result = c.execute(
            "DELETE FROM notifications WHERE id=? AND managed=1",
            (notification_id,),
        )
        if not result.rowcount:
            abort(404)
    flash("Fecha importante eliminada.", "success")
    return redirect(url_for("notifications"))


@app.route("/logros")
@require("student")
def student_achievements():
    (
        student, total, completed, reviewed, average, achievements, _, on_time, improved,
    ) = _student_learning_data(session["user_id"])
    return render_template(
        "student_achievements.html",
        student=student,
        achievements=achievements,
        earned_count=sum(item["earned"] for item in achievements),
        total=total,
        completed=completed,
        reviewed=reviewed,
        average=average,
        on_time=on_time,
        improved=improved,
    )


@app.route("/configuracion", methods=["GET", "POST"])
@require()
def student_settings():
    user_id = session["user_id"]
    with connect() as c:
        preference_row = c.execute(
            """SELECT theme,appearance,text_size,reduce_motion,dashboard_widgets,
                      dashboard_widgets_version,
                      notification_types,favorite_links,calendar_view,interface_size,show_cecyto,
                      profile_avatar
               FROM user_preferences WHERE user_id=?""",
            (user_id,),
        ).fetchone()
        preferences = dict(DEFAULT_USER_PREFERENCES)
        available_widgets = DEFAULT_DASHBOARD_WIDGETS[session["role"]]
        dashboard_widgets = list(available_widgets)
        notification_types = list(NOTIFICATION_OPTIONS)
        favorite_links = []
        favorite_options = FAVORITE_OPTIONS[session["role"]]
        if preference_row:
            stored_preferences = dict(preference_row)
            preferences.update({
                key: value for key, value in stored_preferences.items()
                if key not in {
                    "dashboard_widgets", "notification_types", "favorite_links"
                }
            })
            dashboard_widgets = _dashboard_widgets_for(
                session["role"],
                stored_preferences["dashboard_widgets"],
                stored_preferences["dashboard_widgets_version"],
            )
            notification_types = _preference_list(
                stored_preferences["notification_types"],
                NOTIFICATION_OPTIONS,
                "notificaciones",
            )
            favorite_links = _preference_list(
                stored_preferences["favorite_links"],
                favorite_options,
                "accesos favoritos",
                default_values=(),
            )

        preferences["profile_avatar"] = (
            preferences["profile_avatar"] or DEFAULT_PROFILE_AVATAR
        )

        if request.method == "POST":
            allowed_values = {
                "theme": {"green", "blue", "violet", "rose", "amber"},
                "appearance": {"system", "light", "dark"},
                "text_size": {"normal", "large", "xlarge"},
            }
            updates = {}
            for name, valid_values in allowed_values.items():
                if name in request.form:
                    value = request.form.get(name, "")
                    if value not in valid_values:
                        abort(400, description=f"La opción de {name} no es válida.")
                    updates[name] = value
            if "settings_form" in request.form:
                updates["reduce_motion"] = int(request.form.get("reduce_motion") == "on")
                if request.form.get("dashboard_widgets_submitted") != "1":
                    abort(400, description="No se recibieron las opciones del panel.")
                if request.form.get("notification_types_submitted") != "1":
                    abort(400, description="No se recibieron las opciones de notificaciones.")
                if request.form.get("favorite_links_submitted") != "1":
                    abort(400, description="No se recibieron los accesos favoritos.")
                dashboard_widgets = request.form.getlist("dashboard_widgets")
                if any(widget not in available_widgets for widget in dashboard_widgets):
                    abort(400, description="Uno de los bloques del panel no es válido.")
                dashboard_widgets = list(dict.fromkeys(dashboard_widgets))
                updates["dashboard_widgets"] = json.dumps(dashboard_widgets)
                notification_types = request.form.getlist("notification_types")
                if any(kind not in NOTIFICATION_OPTIONS for kind in notification_types):
                    abort(400, description="Uno de los avisos seleccionados no es válido.")
                notification_types = list(dict.fromkeys(notification_types))
                updates["notification_types"] = json.dumps(notification_types)
                favorite_links = request.form.getlist("favorite_links")
                if any(link not in favorite_options for link in favorite_links):
                    abort(400, description="Uno de los accesos favoritos no es válido.")
                favorite_links = list(dict.fromkeys(favorite_links))
                if len(favorite_links) > 3:
                    abort(400, description="Puedes guardar hasta tres accesos favoritos.")
                updates["favorite_links"] = json.dumps(favorite_links)
                calendar_view = request.form.get("calendar_view", "")
                if calendar_view not in {"month", "list"}:
                    abort(400, description="La forma de ver el calendario no es válida.")
                preferences["calendar_view"] = calendar_view
                interface_size = request.form.get("interface_size", "")
                if interface_size not in {"normal", "large"}:
                    abort(400, description="El tamaño de los elementos no es válido.")
                preferences["interface_size"] = interface_size
                preferences["show_cecyto"] = int(
                    request.form.get("show_cecyto") == "on"
                )
                profile_avatar = request.form.get("profile_avatar", "")
                if not profile_avatar:
                    profile_avatar = DEFAULT_PROFILE_AVATAR
                if (
                    profile_avatar != INITIAL_PROFILE_AVATAR
                    and profile_avatar not in PROFILE_AVATARS.values()
                ):
                    abort(400, description="La imagen de perfil seleccionada no es válida.")
                preferences["profile_avatar"] = profile_avatar
            elif "reduce_motion" in request.form:
                abort(400, description="La opción de movimiento no es válida.")
            if not updates:
                abort(400, description="No se recibieron preferencias para guardar.")

            preferences.update(updates)
            c.execute(
                """INSERT INTO user_preferences
                   (user_id,theme,appearance,text_size,reduce_motion,dashboard_widgets,
                    notification_types,favorite_links,calendar_view,interface_size,show_cecyto,
                    dashboard_widgets_version,profile_avatar)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     theme=excluded.theme,
                     appearance=excluded.appearance,
                     text_size=excluded.text_size,
                     reduce_motion=excluded.reduce_motion,
                     dashboard_widgets=excluded.dashboard_widgets,
                     notification_types=excluded.notification_types,
                     favorite_links=excluded.favorite_links,
                     calendar_view=excluded.calendar_view,
                     interface_size=excluded.interface_size,
                     show_cecyto=excluded.show_cecyto,
                     dashboard_widgets_version=excluded.dashboard_widgets_version,
                     profile_avatar=excluded.profile_avatar""",
                (
                    user_id,
                    preferences["theme"],
                    preferences["appearance"],
                    preferences["text_size"],
                    preferences["reduce_motion"],
                    json.dumps(dashboard_widgets),
                    json.dumps(notification_types),
                    json.dumps(favorite_links),
                    preferences["calendar_view"],
                    preferences["interface_size"],
                    preferences["show_cecyto"],
                    1,
                    preferences["profile_avatar"],
                ),
            )
            flash("Tus preferencias personales se guardaron correctamente.", "success")
            return redirect(url_for("student_settings"))

    student = None
    if session.get("role") == "student":
        student, _, _, _, _, _, _, _, _ = _student_learning_data(user_id)
    return render_template(
        "student_settings.html",
        student=student,
        preferences=preferences,
        dashboard_widgets=dashboard_widgets,
        notification_types=notification_types,
        notification_options=NOTIFICATION_OPTIONS,
        favorite_links=favorite_links,
        favorite_options=favorite_options,
        profile_avatars=PROFILE_AVATARS,
        initial_profile_avatar=INITIAL_PROFILE_AVATAR,
    )


@app.post("/celebraciones/vistas")
@require("student")
def mark_celebrations_seen():
    try:
        celebration_ids = json.loads(request.form.get("ids", "[]"))
    except json.JSONDecodeError:
        abort(400, description="La lista de felicitaciones no es válida.")
    if not isinstance(celebration_ids, list) or len(celebration_ids) > 12 or any(
        not isinstance(item, int) or isinstance(item, bool) or item < 1
        for item in celebration_ids
    ):
        abort(400, description="La lista de felicitaciones no es válida.")
    if celebration_ids:
        placeholders = ",".join("?" for _ in celebration_ids)
        with connect() as c:
            c.execute(
                f"""UPDATE student_celebrations SET seen_at=CURRENT_TIMESTAMP
                    WHERE student_id=? AND seen_at IS NULL AND id IN ({placeholders})""",
                (session["user_id"], *celebration_ids),
            )
    return "", 204


def _student_directory_return(source):
    return_q = source.get("return_q", "").strip()
    return_group = source.get("return_group", "").strip().upper()[:20]
    return_semester = source.get("return_semester", "").strip()
    if return_semester not in {"1", "2", "3", "4", "5", "6", "none"}:
        return_semester = ""
    return_status = source.get("return_status", "").strip()
    if return_status not in {"active", "inactive"}:
        return_status = ""
    return {
        "return_q": return_q,
        "return_group": return_group,
        "return_semester": return_semester,
        "return_status": return_status,
        "return_url": url_for(
            "students",
            q=return_q or None,
            group=return_group or None,
            semester=return_semester or None,
            status=return_status or None,
        ),
    }


@app.route("/alumnos")
@require("teacher")
def students():
    q = request.args.get("q", "").strip()
    group = request.args.get("group", "").strip().upper()
    semester = request.args.get("semester", "").strip()
    status = request.args.get("status", "").strip()
    sql = "SELECT * FROM users WHERE role='student' AND approved=1"
    args = []
    if q:
        sql += (
            " AND (LOWER(full_name) LIKE LOWER(?)"
            " OR CAST(list_number AS TEXT) LIKE ?"
            " OR LOWER(group_name) LIKE LOWER(?))"
        )
        args.extend((f"%{q}%", f"%{q}%", f"%{q}%"))
    if group:
        sql += " AND group_name=?"
        args.append(group)
    if semester in {"1", "2", "3", "4", "5", "6"}:
        sql += " AND semester=?"
        args.append(int(semester))
    elif semester == "none":
        sql += " AND semester IS NULL"
    else:
        semester = ""
    if not group or not semester:
        sql += " AND 1=0"
    if status in {"active", "inactive"}:
        sql += " AND active=?"
        args.append(1 if status == "active" else 0)
    else:
        status = ""
    quick_group = request.args.get("quick_group", "").strip().upper()
    quick_semester = request.args.get("quick_semester", "").strip()
    quick_number = request.args.get("quick_list_number", "").strip()
    quick_student = None
    quick_message = ""
    if quick_group or quick_semester or quick_number:
        if (
            not quick_group
            or quick_semester not in {"1", "2", "3", "4", "5", "6", "none"}
            or not quick_number.isdigit()
        ):
            quick_message = "Selecciona un grupo y semestre, y escribe un número de lista válido."
        else:
            with connect() as c:
                semester_clause = (
                    "semester IS NULL" if quick_semester == "none" else "semester=?"
                )
                quick_parameters = (
                    (quick_group, int(quick_number))
                    if quick_semester == "none"
                    else (quick_group, int(quick_semester), int(quick_number))
                )
                quick_student = c.execute(
                    f"""SELECT * FROM users
                       WHERE role='student' AND approved=1 AND active=1
                         AND group_name=? AND {semester_clause} AND list_number=?""",
                    quick_parameters,
                ).fetchone()
            if not quick_student:
                quick_message = "No se encontró un alumno activo con ese grupo, semestre y número de lista."
    sql += (
        " ORDER BY LOWER(group_name), "
        "semester NULLS LAST, list_number NULLS LAST, "
        "LOWER(COALESCE(NULLIF(last_name,''),full_name)), "
        "LOWER(COALESCE(NULLIF(first_name,''),full_name)), id"
    )
    with connect() as c:
        rows = c.execute(sql, args).fetchall()
        group_stats = c.execute(
            """SELECT group_name,semester,
                      COUNT(*) FILTER (WHERE active=1) AS active_students,
                      COUNT(*) FILTER (WHERE active=0) AS inactive_students
               FROM users
               WHERE role='student' AND approved=1
               GROUP BY group_name,semester
               ORDER BY LOWER(group_name),semester"""
        ).fetchall()
        groups = c.execute(
            """SELECT group_name FROM users
               WHERE role='student' AND approved=1
               GROUP BY group_name
               ORDER BY LOWER(group_name)"""
        ).fetchall()
        semester_options = c.execute(
            """SELECT DISTINCT semester FROM users
               WHERE role='student' AND approved=1 AND semester IS NOT NULL
               ORDER BY semester"""
        ).fetchall()
        directory_stats = {
            "total": c.execute(
                "SELECT COUNT(*) FROM users WHERE role='student' AND approved=1"
            ).fetchone()[0],
            "active": c.execute(
                "SELECT COUNT(*) FROM users WHERE role='student' AND approved=1 AND active=1"
            ).fetchone()[0],
            "inactive": c.execute(
                "SELECT COUNT(*) FROM users WHERE role='student' AND approved=1 AND active=0"
            ).fetchone()[0],
            "pending": c.execute(
                "SELECT COUNT(*) FROM users WHERE role='student' AND approved=0 AND active=1"
            ).fetchone()[0],
        }
    return render_template(
        "students.html", students=rows, groups=groups, group_stats=group_stats,
        semester_options=semester_options, selected_semester=semester,
        selected_status=status, directory_stats=directory_stats,
        q=q, selected_group=group,
        cohort_selected=bool(group and semester),
        quick_student=quick_student, quick_message=quick_message,
        quick_group=quick_group, quick_semester=quick_semester,
        quick_list_number=quick_number,
    )


@app.route("/alumno/<int:uid>/editar", methods=["GET", "POST"])
@require("teacher")
def edit_student(uid):
    return_source = request.form if request.method == "POST" else request.args
    return_context = _student_directory_return(return_source)
    with connect() as c:
        student = c.execute(
            "SELECT * FROM users WHERE id=? AND role='student' AND approved=1",
            (uid,),
        ).fetchone()
        if not student:
            abort(404)
        if request.method == "POST":
            first_name = request.form.get("first_name", "").strip()
            last_name = request.form.get("last_name", "").strip()
            full_name = f"{first_name} {last_name}".strip()
            group = request.form.get("group_name", "").strip().upper()
            list_number = request.form.get("list_number", "").strip()
            semester = request.form.get("semester", "").strip()
            email = request.form.get("email", "").strip().lower()
            if not all([first_name, last_name, group, list_number, semester, email]):
                flash("Completa todos los campos.", "error")
            elif len(first_name) > 80 or len(last_name) > 100 or len(group) > 20:
                flash("Revisa la longitud del nombre, apellidos o grupo.", "error")
            elif not re.fullmatch(
                r"[^@\s]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}",
                email,
            ) or len(email) > 254:
                flash("Escribe un correo electrónico válido.", "error")
            elif not list_number.isdigit() or len(list_number) > 4 or int(list_number) < 1:
                flash("El número de lista debe ser un entero positivo de hasta 4 cifras.", "error")
            elif semester not in {"1", "2", "3", "4", "5", "6"}:
                flash("El semestre debe estar entre 1 y 6.", "error")
            elif c.execute(
                "SELECT id FROM users WHERE id<>? AND (lower(email)=? OR lower(username)=?)",
                (uid, email, email),
            ).fetchone():
                flash("Ese correo ya está asociado a otra cuenta.", "error")
            elif c.execute(
                """SELECT id FROM users WHERE id<>? AND role='student'
                   AND group_name=? AND semester=? AND list_number=?""",
                (uid, group, int(semester), int(list_number)),
            ).fetchone():
                flash("Ese número de lista ya está registrado en ese grupo y semestre.", "error")
            else:
                previous_group = student["group_name"]
                previous_semester = student["semester"]
                c.execute(
                    """UPDATE users SET full_name=?,first_name=?,last_name=?,email=?,
                       group_name=?,list_number=?,semester=? WHERE id=? AND role='student'""",
                    (full_name, first_name, last_name, email, group, int(list_number), int(semester), uid),
                )
                if group != previous_group or int(semester) != previous_semester:
                    c.execute(
                        """DELETE FROM grades WHERE student_id=? AND grade IS NULL
                           AND status='Pendiente'
                           AND activity_id IN (
                             SELECT id FROM activities
                             WHERE group_name<>? OR semester<>? OR semester IS NULL
                           )""",
                        (uid, group, int(semester)),
                    )
                    if student["active"]:
                        activities_for_group = c.execute(
                            """SELECT id FROM activities
                               WHERE group_name=? AND semester=?""",
                            (group, int(semester)),
                        ).fetchall()
                        c.executemany(
                            "INSERT OR IGNORE INTO grades(student_id,activity_id,status) VALUES(?,?,'Pendiente')",
                            [(uid, activity["id"]) for activity in activities_for_group],
                        )
                flash("Datos del alumno actualizados.", "success")
                return redirect(return_context["return_url"])
    groups = []
    with connect() as c:
        groups = c.execute(
            "SELECT DISTINCT group_name FROM users WHERE role='student' AND approved=1 ORDER BY group_name"
        ).fetchall()
    return render_template(
        "student_edit.html", student=student, groups=groups, **return_context
    )


@app.post("/alumno/<int:uid>/estado")
@require("teacher")
def toggle_student_active(uid):
    return_context = _student_directory_return(request.form)
    with connect() as c:
        student = c.execute(
            "SELECT id,active FROM users WHERE id=? AND role='student' AND approved=1",
            (uid,),
        ).fetchone()
        if not student:
            abort(404)
        active = 0 if student["active"] else 1
        c.execute("UPDATE users SET active=? WHERE id=?", (active, uid))
    flash(
        "Cuenta del alumno desactivada." if not active else "Cuenta del alumno reactivada.",
        "success",
    )
    return redirect(return_context["return_url"])


@app.route("/pendientes")
@require("teacher")
def pending():
    selected_group = request.args.get("group", "").strip().upper()
    selected_semester = request.args.get("semester", "").strip()
    if selected_semester not in {"1", "2", "3", "4", "5", "6"}:
        selected_semester = ""
    with connect() as c:
        conditions = ["role='student'", "approved=0", "active=1"]
        parameters = []
        if selected_group and selected_semester:
            conditions.extend(["group_name=?", "semester=?"])
            parameters.extend((selected_group, int(selected_semester)))
        else:
            conditions.append("1=0")
        rows = c.execute(
            """SELECT * FROM users WHERE """
            + " AND ".join(conditions)
            + """ ORDER BY LOWER(group_name),semester NULLS LAST,list_number NULLS LAST,
                        LOWER(COALESCE(NULLIF(last_name,''),full_name)),
                        LOWER(COALESCE(NULLIF(first_name,''),full_name)),
                        id""",
            parameters,
        ).fetchall()
        groups = c.execute(
            """SELECT group_name FROM users WHERE role='student'
               AND approved=0 AND active=1 GROUP BY group_name
               ORDER BY LOWER(group_name)"""
        ).fetchall()
        semester_options = c.execute(
            """SELECT DISTINCT semester FROM users WHERE role='student'
               AND approved=0 AND active=1 AND semester IS NOT NULL ORDER BY semester"""
        ).fetchall()
    return render_template(
        "pending.html", students=rows, groups=groups,
        semester_options=semester_options, selected_group=selected_group,
        selected_semester=selected_semester,
    )

@app.post("/aprobar/<int:uid>")
@require("teacher")
def approve(uid):
    with connect() as c:
        student=c.execute(
            """SELECT id,group_name,semester FROM users
               WHERE id=? AND role='student' AND active=1""",
            (uid,),
        ).fetchone()
        if student:
            c.execute("UPDATE users SET approved=1 WHERE id=?",(uid,))
            existing=c.execute(
                """SELECT id FROM activities WHERE group_name=? AND semester=?""",
                (student["group_name"], student["semester"]),
            ).fetchall()
            c.executemany("INSERT OR IGNORE INTO grades(student_id,activity_id,status) VALUES(?,?,'Pendiente')",
                          [(uid,a["id"]) for a in existing])
    flash("Alumno aprobado. Ya puede iniciar sesión.","success")
    return redirect(url_for(
        "pending",
        group=request.form.get("group", "").strip().upper(),
        semester=request.form.get("semester", "").strip(),
    ))

@app.post("/rechazar/<int:uid>")
@require("teacher")
def reject(uid):
    with connect() as c:
        c.execute("DELETE FROM users WHERE id=? AND role='student' AND approved=0",(uid,))
    flash("Solicitud eliminada.","success")
    return redirect(url_for(
        "pending",
        group=request.form.get("group", "").strip().upper(),
        semester=request.form.get("semester", "").strip(),
    ))

@app.route("/actividades",methods=["GET","POST"])
@require()
def activities():
    if request.method=="POST":
        if session["role"] != "teacher":
            abort(403)
        vals=[request.form.get(k,"").strip() for k in ["group_name","subject","partial","title"]]
        semester_raw = request.form.get("semester", "").strip()
        semester = int(semester_raw) if semester_raw in {"1", "2", "3", "4", "5", "6"} else None
        desc=request.form.get("description","").strip(); due=request.form.get("due_date","").strip()
        vals[0] = vals[0].upper()
        if not all(vals) or semester is None:
            flash("Completa grupo, semestre, materia, parcial y título.","error")
        elif any(len(value) > 160 for value in vals[1:]) or len(desc) > 5000:
            flash("Revisa la longitud del título, materia, parcial o instrucciones.", "error")
        elif due and not _valid_iso_date(due):
            flash("La fecha de entrega no es válida.", "error")
        else:
            with connect() as c:
                if not c.execute(
                    """SELECT 1 FROM users WHERE role='student' AND approved=1 AND active=1
                       AND group_name=? AND semester=? LIMIT 1""",
                    (vals[0], semester),
                ).fetchone():
                    flash("No hay alumnos activos aprobados en ese grupo y semestre.", "error")
                    return redirect(url_for("activities"))
                cur=c.execute(
                    """INSERT INTO activities
                       (group_name,semester,subject,partial,title,description,due_date,created_by)
                       VALUES(?,?,?,?,?,?,?,?) RETURNING id""",
                    (vals[0], semester, vals[1], vals[2], vals[3], desc, due or None, session["user_id"]),
                )
                activity_id = cur.fetchone()["id"]
                students=c.execute(
                    """SELECT id FROM users WHERE role='student' AND approved=1 AND active=1
                       AND group_name=? AND semester=?""",
                    (vals[0], semester),
                ).fetchall()
                c.executemany("INSERT OR IGNORE INTO grades(student_id,activity_id,status) VALUES(?,?,'Pendiente')",[(s["id"],activity_id) for s in students])
                _create_notification(
                    c,
                    event_type="activity",
                    title="Nueva actividad publicada",
                    message=f"{vals[3]} · {vals[1]}",
                    event_date=due or None,
                    target_group=vals[0],
                    target_semester=semester,
                    activity_id=activity_id,
                    created_by=session["user_id"],
                )
            flash("Actividad publicada para el grupo y semestre seleccionados.","success"); return redirect(url_for("activities"))
    selected_status = request.args.get("status", "").strip()
    selected_deadline = request.args.get("deadline", "").strip()
    selected_group = request.args.get("group", "").strip().upper()
    selected_semester = request.args.get("semester", "").strip()
    if selected_semester not in {"1", "2", "3", "4", "5", "6"}:
        selected_semester = ""
    allowed_statuses = {"Pendiente", "Entregada", "Revisada"} if session["role"] == "student" else {
        "pending", "submitted", "reviewed", "unassigned"
    }
    if selected_status not in allowed_statuses:
        selected_status = ""
    if selected_deadline not in {"upcoming", "overdue", "undated"}:
        selected_deadline = ""
    today = date.today().isoformat()
    with connect() as c:
        groups = c.execute(
            "SELECT DISTINCT group_name FROM users WHERE role='student' AND approved=1 AND active=1 ORDER BY group_name"
        ).fetchall()
        semester_options = c.execute(
            """SELECT DISTINCT semester FROM users
               WHERE role='student' AND approved=1 AND active=1 AND semester IS NOT NULL
               ORDER BY semester"""
        ).fetchall()
        if session["role"] == "teacher":
            sql = """SELECT a.*,
                            (SELECT COUNT(*) FROM grades g
                             JOIN users u ON u.id=g.student_id
                             WHERE g.activity_id=a.id AND u.role='student'
                               AND u.approved=1 AND u.group_name=a.group_name
                               AND u.semester=a.semester) AS assigned_count,
                            (SELECT COUNT(*) FROM grades g
                             JOIN users u ON u.id=g.student_id
                             WHERE g.activity_id=a.id AND u.role='student'
                               AND u.approved=1 AND u.group_name=a.group_name
                               AND u.semester=a.semester AND g.status='Pendiente') AS pending_count,
                            (SELECT COUNT(*) FROM grades g
                             JOIN users u ON u.id=g.student_id
                             WHERE g.activity_id=a.id AND u.role='student'
                               AND u.approved=1 AND u.group_name=a.group_name
                               AND u.semester=a.semester AND g.status='Entregada') AS submitted_count,
                            (SELECT COUNT(*) FROM grades g
                             JOIN users u ON u.id=g.student_id
                             WHERE g.activity_id=a.id AND u.role='student'
                               AND u.approved=1 AND u.group_name=a.group_name
                               AND u.semester=a.semester AND g.status='Revisada') AS reviewed_count
                     FROM activities a"""
            conditions = []
            params = []
            if bool(selected_group) != bool(selected_semester):
                conditions.append("1=0")
            elif selected_group and selected_semester:
                conditions.extend(["a.group_name=?", "a.semester=?"])
                params.extend((selected_group, int(selected_semester)))
            if selected_status:
                status_map = {
                    "pending": "Pendiente",
                    "submitted": "Entregada",
                    "reviewed": "Revisada",
                }
                if selected_status == "unassigned":
                    conditions.append(
                        """NOT EXISTS (SELECT 1 FROM grades g
                                      JOIN users u ON u.id=g.student_id
                                      WHERE g.activity_id=a.id AND u.role='student'
                                        AND u.approved=1 AND u.group_name=a.group_name
                                        AND u.semester=a.semester)"""
                    )
                else:
                    conditions.append(
                        """EXISTS (SELECT 1 FROM grades g
                                   JOIN users u ON u.id=g.student_id
                                   WHERE g.activity_id=a.id AND u.role='student'
                                     AND u.approved=1 AND u.group_name=a.group_name
                                     AND u.semester=a.semester AND g.status=?)"""
                    )
                    params.append(status_map[selected_status])
        else:
            student = c.execute(
                "SELECT group_name,semester FROM users WHERE id=?",
                (session["user_id"],),
            ).fetchone()
            sql = """SELECT a.*,g.grade,g.status
                     FROM activities a
                     LEFT JOIN grades g ON g.activity_id=a.id AND g.student_id=?"""
            params = [session["user_id"]]
            conditions = ["((a.group_name=? AND a.semester=?) OR g.id IS NOT NULL)"]
            params.extend((student["group_name"], student["semester"]))
            if selected_status:
                conditions.append("COALESCE(g.status,'Pendiente')=?")
                params.append(selected_status)
        if selected_deadline == "upcoming":
            conditions.append("a.due_date>=?")
            params.append(today)
        elif selected_deadline == "overdue":
            conditions.append("a.due_date<?")
            params.append(today)
        elif selected_deadline == "undated":
            conditions.append("a.due_date IS NULL")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY COALESCE(a.due_date,'9999-12-31'),a.id DESC"
        rows = c.execute(sql, params).fetchall()
    return render_template(
        "activities.html", activities=rows, groups=groups,
        semester_options=semester_options,
        selected_status=selected_status, selected_deadline=selected_deadline,
        selected_group=selected_group, selected_semester=selected_semester,
        today=today,
    )


def _valid_iso_date(value):
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


@app.route("/actividad/<int:aid>/editar", methods=["GET", "POST"])
@require("teacher")
def edit_activity(aid):
    return_source = request.form if request.method == "POST" else request.args
    return_status = return_source.get("return_status", "").strip()
    if return_status not in {"pending", "submitted", "reviewed", "unassigned"}:
        return_status = ""
    return_deadline = return_source.get("return_deadline", "").strip()
    if return_deadline not in {"upcoming", "overdue", "undated"}:
        return_deadline = ""
    return_group = return_source.get("return_group", "").strip().upper()[:20]
    return_semester = return_source.get("return_semester", "").strip()
    if return_semester not in {"1", "2", "3", "4", "5", "6"}:
        return_semester = ""
    return_url = url_for(
        "activities",
        status=return_status or None,
        deadline=return_deadline or None,
        group=return_group or None,
        semester=return_semester or None,
    )
    with connect() as c:
        activity = c.execute("SELECT * FROM activities WHERE id=?", (aid,)).fetchone()
        if not activity:
            abort(404)
        if request.method == "POST":
            group = request.form.get("group_name", "").strip().upper()
            semester_raw = request.form.get("semester", "").strip()
            semester = int(semester_raw) if semester_raw in {"1", "2", "3", "4", "5", "6"} else None
            subject = request.form.get("subject", "").strip()
            partial = request.form.get("partial", "").strip()
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            due_date = request.form.get("due_date", "").strip()
            if not all([group, subject, partial, title]) or semester is None:
                flash("Completa grupo, semestre, materia, parcial y título.", "error")
            elif any(len(value) > 160 for value in (subject, partial, title)) or len(description) > 5000:
                flash("Revisa la longitud de los campos de la actividad.", "error")
            elif due_date and not _valid_iso_date(due_date):
                flash("La fecha de entrega no es válida.", "error")
            elif (group, semester) != (activity["group_name"], activity["semester"]) and not c.execute(
                """SELECT 1 FROM users WHERE role='student' AND approved=1 AND active=1
                   AND group_name=? AND semester=? LIMIT 1""",
                (group, semester),
            ).fetchone():
                flash("No hay alumnos activos aprobados en ese grupo y semestre.", "error")
            else:
                old_group = activity["group_name"]
                old_semester = activity["semester"]
                cohort_changed = (group, semester) != (old_group, old_semester)
                changed = (
                    cohort_changed
                    or subject != activity["subject"]
                    or partial != activity["partial"]
                    or title != activity["title"]
                    or description != (activity["description"] or "")
                    or (due_date or None) != activity["due_date"]
                )
                c.execute(
                    """UPDATE activities SET group_name=?,semester=?,subject=?,partial=?,title=?,
                       description=?,due_date=? WHERE id=?""",
                    (group, semester, subject, partial, title, description, due_date or None, aid),
                )
                if cohort_changed:
                    c.execute(
                        """DELETE FROM grades WHERE activity_id=? AND grade IS NULL
                           AND status='Pendiente'
                           AND student_id IN (
                             SELECT id FROM users WHERE role='student'
                               AND (group_name<>? OR semester<>? OR semester IS NULL)
                           )""",
                        (aid, group, semester),
                    )
                    students = c.execute(
                        """SELECT id FROM users WHERE role='student' AND approved=1 AND active=1
                           AND group_name=? AND semester=?""",
                        (group, semester),
                    ).fetchall()
                    c.executemany(
                        "INSERT OR IGNORE INTO grades(student_id,activity_id,status) VALUES(?,?,'Pendiente')",
                        [(student["id"], aid) for student in students],
                    )
                    _create_notification(
                        c,
                        event_type="activity_update",
                        title="Actividad modificada",
                        message=f"{title} · ahora asignada al grupo {group}, semestre {semester}",
                        event_date=due_date or None,
                        target_group=group,
                        target_semester=semester,
                        activity_id=aid,
                        created_by=session["user_id"],
                    )
                    _create_notification(
                        c,
                        event_type="activity_update",
                        title="Actividad reasignada",
                        message=(
                            f"{activity['title']} ya no corresponde al grupo {old_group}, "
                            f"semestre {old_semester or 'sin asignar'}."
                        ),
                        target_group=old_group,
                        target_semester=old_semester,
                        activity_id=aid,
                        created_by=session["user_id"],
                    )
                elif changed:
                    _create_notification(
                        c,
                        event_type="activity_update",
                        title="Actividad modificada",
                        message=f"{title} · {subject}" + (f" · entrega: {due_date}" if due_date else ""),
                        event_date=due_date or None,
                        target_group=group,
                        target_semester=semester,
                        activity_id=aid,
                        created_by=session["user_id"],
                    )
                flash("Actividad actualizada. Las calificaciones existentes se conservaron.", "success")
                return redirect(return_url)
        groups = c.execute(
            "SELECT DISTINCT group_name FROM users WHERE role='student' AND approved=1 AND active=1 ORDER BY group_name"
        ).fetchall()
        semester_options = c.execute(
            """SELECT DISTINCT semester FROM users
               WHERE role='student' AND approved=1 AND active=1 AND semester IS NOT NULL
               ORDER BY semester"""
        ).fetchall()
    return render_template(
        "activity_edit.html", activity=activity, groups=groups,
        semester_options=semester_options, return_url=return_url,
        return_status=return_status, return_deadline=return_deadline,
        return_group=return_group, return_semester=return_semester,
    )

@app.post("/actividad/<int:aid>/eliminar")
@require("teacher")
def delete_activity(aid):
    with connect() as c: c.execute("DELETE FROM activities WHERE id=?",(aid,))
    flash("Actividad eliminada.","success"); return redirect(url_for("activities"))

@app.route("/calificaciones")
@require()
def grades():
    selected_group = request.args.get("group", "").strip().upper()
    selected_semester = request.args.get("semester", "").strip()
    if selected_semester not in {"1", "2", "3", "4", "5", "6"}:
        selected_semester = ""
    with connect() as c:
        sql="""SELECT u.full_name,u.first_name,u.last_name,u.group_name,u.list_number,a.subject,a.partial,a.title,a.due_date,
                      u.semester,g.grade,g.status,g.comments,g.submitted_at,g.id grade_id
               FROM grades g JOIN users u ON u.id=g.student_id JOIN activities a ON a.id=g.activity_id"""
        if session["role"]=="student":
            rows=c.execute(sql+" WHERE u.id=? ORDER BY a.id DESC",(session["user_id"],)).fetchall()
        else:
            if selected_group and selected_semester:
                rows = c.execute(
                    sql + """ WHERE u.group_name=? AND u.semester=?
                            AND a.group_name=u.group_name AND a.semester=u.semester
                          ORDER BY LOWER(u.group_name),
                          u.semester NULLS LAST,u.list_number NULLS LAST,
                          LOWER(COALESCE(NULLIF(u.last_name,''),u.full_name)),
                          LOWER(COALESCE(NULLIF(u.first_name,''),u.full_name)),
                          u.id, a.id DESC""",
                    (selected_group, int(selected_semester)),
                ).fetchall()
            else:
                rows = []
        groups = c.execute(
            """SELECT group_name FROM users WHERE role='student'
               AND approved=1 AND active=1 GROUP BY group_name
               ORDER BY LOWER(group_name)"""
        ).fetchall() if session["role"] == "teacher" else []
        semester_options = c.execute(
            """SELECT DISTINCT semester FROM users WHERE role='student'
               AND approved=1 AND active=1 AND semester IS NOT NULL ORDER BY semester"""
        ).fetchall() if session["role"] == "teacher" else []
    return render_template(
        "grades.html", rows=rows, groups=groups,
        semester_options=semester_options, selected_group=selected_group,
        selected_semester=selected_semester,
    )


def _persist_grade_update(c, gid, previous, value, status, comments, submitted_at_raw):
    result = c.execute(
        """UPDATE grades SET grade=?,status=?,comments=?,submitted_at=?,
           reviewed_at=CASE WHEN ?='Revisada' THEN CURRENT_TIMESTAMP ELSE NULL END
           WHERE id=?""",
        (
            value, status, comments,
            submitted_at_raw if status in ("Entregada", "Revisada") else None,
            status, gid,
        ),
    )
    if result.rowcount == 0:
        abort(404)
    if previous["grade"] is not None and value is not None and value > previous["grade"]:
        c.execute(
            """INSERT INTO grade_improvements(student_id,activity_id,previous_grade,grade)
               VALUES(?,?,?,?)""",
            (previous["student_id"], previous["activity_id"], previous["grade"], value),
        )
    if value is not None and value != previous["grade"]:
        _create_notification(
            c,
            event_type="grade",
            title="Calificación registrada o actualizada",
            message=f"{previous['full_name']} · {previous['title']} · nueva calificación: {value:g}/10.",
            event_date=date.today().isoformat(),
            target_student=previous["student_id"],
            activity_id=previous["activity_id"],
            created_by=session["user_id"],
        )
    if (
        previous["status"] not in ("Entregada", "Revisada")
        and status in ("Entregada", "Revisada")
    ):
        submitted_date = submitted_at_raw or date.today().isoformat()
        c.execute(
            """INSERT OR IGNORE INTO student_celebrations
               (student_id,event_key,title,message) VALUES(?,?,?,?)""",
            (
                previous["student_id"],
                f"activity-completed:{gid}",
                "¡Actividad completada!",
                f"El docente confirmó la entrega de «{previous['title']}».",
            ),
        )
        _create_notification(
            c,
            event_type="completion",
            title="Actividad completada",
            message=f"{previous['full_name']}: el docente confirmó la entrega de «{previous['title']}».",
            event_date=submitted_date,
            target_student=previous["student_id"],
            activity_id=previous["activity_id"],
            created_by=session["user_id"],
        )
    if status in ("Entregada", "Revisada") and not submitted_at_raw:
        c.execute(
            "UPDATE grades SET submitted_at=? WHERE id=?",
            (date.today().isoformat(), gid),
        )


@app.post("/calificacion/<int:gid>")
@require("teacher")
def update_grade(gid):
    def grade_redirect(student_id=None):
        target_id = student_id or request.form.get("student_id", type=int)
        if request.form.get("return_to") == "record" and target_id:
            return redirect(url_for("record", student_id=target_id))
        return redirect(url_for("grades"))

    stamp_evidence = request.form.get("action") == "stamp"
    if stamp_evidence:
        status = "Revisada"
        comments = ""
        submitted_at_raw = ""
        value = None
    else:
        raw=request.form.get("grade","").strip()
        status=request.form.get("status","Pendiente")
        comments=request.form.get("comments","").strip()
        submitted_at_raw = request.form.get("submitted_at", "").strip()
        try:
            value=float(raw) if raw else None
            if value is not None and not 0<=value<=10: raise ValueError
        except ValueError:
            flash("La calificación debe estar entre 0 y 10.","error"); return grade_redirect()
        if status not in ["Pendiente","Entregada","Revisada"]:
            flash("Selecciona un estado válido.", "error")
            return grade_redirect()
        if submitted_at_raw and not _valid_iso_date(submitted_at_raw):
            flash("La fecha real de entrega no es válida.", "error")
            return grade_redirect()
        if submitted_at_raw and date.fromisoformat(submitted_at_raw) > date.today():
            flash("La fecha real de entrega no puede ser futura.", "error")
            return grade_redirect()
        if submitted_at_raw and status == "Pendiente":
            flash("Registra una fecha real solo cuando la actividad esté entregada o revisada.", "error")
            return grade_redirect()
    with connect() as c:
        previous = c.execute(
            """SELECT g.student_id,g.activity_id,g.grade,g.status,g.comments,g.submitted_at,
                      a.title,a.group_name,a.due_date,u.full_name,u.active,u.approved
               FROM grades g JOIN activities a ON a.id=g.activity_id
               JOIN users u ON u.id=g.student_id WHERE g.id=?""",
            (gid,),
        ).fetchone()
        if not previous:
            abort(404)
        if stamp_evidence:
            if previous["status"] == "Revisada":
                flash("Esta evidencia ya tiene el sello de revisada.", "success")
                return redirect(url_for("record", student_id=previous["student_id"]))
            value = previous["grade"]
            comments = previous["comments"] or ""
            submitted_at_raw = previous["submitted_at"] or date.today().isoformat()
        _persist_grade_update(
            c, gid, previous, value, status, comments, submitted_at_raw
        )
    if previous["active"] and previous["approved"]:
        learning = _student_learning_data(previous["student_id"])
        with connect() as c:
            _sync_student_celebrations(c, previous["student_id"], learning[5])
    if stamp_evidence:
        flash("Evidencia marcada como revisada.", "success")
        return redirect(url_for("record", student_id=previous["student_id"]))
    if value is not None:
        flash("Calificación guardada. El alumno ya puede verla en su cuenta.", "success")
    else:
        flash("Registro actualizado.", "success")
    return grade_redirect(previous["student_id"])


def _batch_grading_data(
    group, semester, activity_id, number_values,
    grade_values=None, status_values=None, comment_values=None,
):
    errors = []
    activities = []
    selected_activity = None
    entries = []
    valid_semester = semester in {"1", "2", "3", "4", "5", "6"}
    number_values = (number_values + [""] * 5)[:5]

    with connect() as c:
        groups = c.execute(
            """SELECT DISTINCT group_name FROM users WHERE role='student'
               AND approved=1 ORDER BY LOWER(group_name)"""
        ).fetchall()
        semester_options = c.execute(
            """SELECT DISTINCT semester FROM users WHERE role='student'
               AND approved=1 AND semester IS NOT NULL ORDER BY semester"""
        ).fetchall()
        if group and valid_semester:
            activities = c.execute(
                """SELECT id,title,subject,partial FROM activities
                   WHERE group_name=? AND semester=? ORDER BY id DESC""",
                (group, int(semester)),
            ).fetchall()
        selected_activity = next(
            (activity for activity in activities if activity["id"] == activity_id),
            None,
        )

        lookup_requested = bool(activity_id or any(number_values))
        if lookup_requested:
            number_ints = []
            if not group or not valid_semester:
                errors.append("Selecciona un grupo y semestre válidos.")
            if not selected_activity:
                errors.append("Selecciona una actividad de ese grupo y semestre.")
            if not all(value.isdigit() for value in number_values):
                errors.append("Ingresa los cinco números de lista.")
            else:
                number_ints = [int(value) for value in number_values]
                if any(number < 1 or number > 9999 for number in number_ints):
                    errors.append("Los números de lista deben estar entre 1 y 9999.")
                elif len(set(number_ints)) != 5:
                    errors.append("Cada alumno debe tener un número de lista distinto.")

            if not errors:
                placeholders = ",".join("?" for _ in number_ints)
                students = c.execute(
                    f"""SELECT u.id,u.full_name,u.list_number,u.group_name,u.semester,
                               u.active,u.approved,g.id grade_id,g.grade,g.status,g.comments
                        FROM users u
                        LEFT JOIN grades g ON g.student_id=u.id AND g.activity_id=?
                        WHERE u.role='student' AND u.approved=1
                          AND u.group_name=? AND u.semester=?
                          AND u.list_number IN ({placeholders})""",
                    (activity_id, group, int(semester), *number_ints),
                ).fetchall()
                students_by_number = {
                    student["list_number"]: student for student in students
                }
                missing_numbers = [
                    number for number in number_ints
                    if number not in students_by_number
                ]
                if missing_numbers:
                    errors.append(
                        "No se encontró alumno aprobado en esa lista con número: "
                        + ", ".join(str(number) for number in missing_numbers)
                        + "."
                    )
                else:
                    for index, number in enumerate(number_ints):
                        student = students_by_number[number]
                        current_grade = (
                            student["grade"]
                            if student["grade"] is not None else ""
                        )
                        if status_values is not None:
                            entry_status = status_values[index]
                        elif student["grade"] is not None:
                            entry_status = student["status"] or "Revisada"
                        else:
                            entry_status = "Revisada"
                        entries.append({
                            "student": student,
                            "number": number,
                            "grade": (
                                grade_values[index]
                                if grade_values is not None
                                else current_grade
                            ),
                            "status": entry_status,
                            "comments": (
                                comment_values[index]
                                if comment_values is not None
                                else student["comments"] or ""
                            ),
                        })

    return {
        "group": group,
        "semester": semester,
        "groups": groups,
        "semester_options": semester_options,
        "activities": activities,
        "activity_id": activity_id,
        "selected_activity": selected_activity,
        "number_values": number_values,
        "entries": entries,
        "errors": errors,
    }


@app.route("/calificar-varios", methods=["GET", "POST"])
@require("teacher")
def batch_grading():
    source = request.form if request.method == "POST" else request.args
    group = source.get("group", "").strip().upper()
    semester = source.get("semester", "").strip()
    if semester not in {"1", "2", "3", "4", "5", "6"}:
        semester = ""
    activity_id = source.get("activity_id", type=int)
    number_values = [
        source.get(f"number_{index}", "").strip()
        for index in range(1, 6)
    ]
    grade_values = [
        source.get(f"grade_{index}", "").strip()
        for index in range(1, 6)
    ] if request.method == "POST" else None
    status_values = [
        source.get(f"status_{index}", "Revisada").strip()
        for index in range(1, 6)
    ] if request.method == "POST" else None
    comment_values = [
        source.get(f"comments_{index}", "").strip()
        for index in range(1, 6)
    ] if request.method == "POST" else None
    page_data = _batch_grading_data(
        group, semester, activity_id, number_values,
        grade_values, status_values, comment_values,
    )
    if request.method == "GET":
        return render_template("batch_grading.html", **page_data)

    errors = list(page_data["errors"])
    parsed_grades = []
    for index, raw_grade in enumerate(grade_values):
        try:
            grade = float(raw_grade)
            if not 0 <= grade <= 10:
                raise ValueError
            parsed_grades.append(grade)
        except ValueError:
            errors.append(f"Revisa la calificación del alumno {index + 1}; debe estar entre 0 y 10.")
        if len(comment_values[index]) > 1000:
            errors.append(f"La observación del alumno {index + 1} supera los 1000 caracteres.")
        if status_values[index] not in {"Pendiente", "Entregada", "Revisada"}:
            errors.append(f"Selecciona un estado válido para el alumno {index + 1}.")

    if not page_data["entries"] and not errors:
        errors.append("Primero busca a los cinco alumnos.")
    if errors:
        page_data["errors"] = errors
        return render_template("batch_grading.html", **page_data)

    saved_students = []
    with connect() as c:
        activity = c.execute(
            """SELECT id FROM activities
               WHERE id=? AND group_name=? AND semester=?""",
            (activity_id, group, int(semester)),
        ).fetchone()
        if not activity:
            errors.append("La actividad ya no pertenece al grupo y semestre seleccionados.")
        number_ints = [int(value) for value in number_values]
        placeholders = ",".join("?" for _ in number_ints)
        students = c.execute(
            f"""SELECT id,full_name,list_number,active,approved
                FROM users WHERE role='student' AND approved=1
                  AND group_name=? AND semester=?
                  AND list_number IN ({placeholders})""",
            (group, int(semester), *number_ints),
        ).fetchall()
        students_by_number = {student["list_number"]: student for student in students}
        if any(number not in students_by_number for number in number_ints):
            errors.append("Uno o más alumnos ya no pertenecen a la lista seleccionada.")
        if not errors:
            for index, number in enumerate(number_ints):
                student = students_by_number[number]
                c.execute(
                    """INSERT OR IGNORE INTO grades(student_id,activity_id,status)
                       VALUES(?,?,'Pendiente')""",
                    (student["id"], activity_id),
                )
                previous = c.execute(
                    """SELECT g.id grade_id,g.student_id,g.activity_id,g.grade,g.status,
                              g.comments,g.submitted_at,a.title,a.group_name,a.due_date,
                              u.full_name,u.active,u.approved
                       FROM grades g JOIN activities a ON a.id=g.activity_id
                       JOIN users u ON u.id=g.student_id
                       WHERE g.student_id=? AND g.activity_id=?""",
                    (student["id"], activity_id),
                ).fetchone()
                status = status_values[index]
                submitted_at = (
                    date.today().isoformat()
                    if status in ("Entregada", "Revisada") else ""
                )
                _persist_grade_update(
                    c, previous["grade_id"], previous, parsed_grades[index],
                    status, comment_values[index], submitted_at,
                )
                saved_students.append(student)

    if errors:
        page_data["errors"] = errors
        return render_template("batch_grading.html", **page_data)

    for student in saved_students:
        if student["active"] and student["approved"]:
            learning = _student_learning_data(student["id"])
            with connect() as c:
                _sync_student_celebrations(c, student["id"], learning[5])
    flash(f"Se guardaron las calificaciones de {len(saved_students)} alumnos.", "success")
    return redirect(url_for(
        "batch_grading",
        group=group,
        semester=semester,
        activity_id=activity_id,
        **{
            f"number_{index + 1}": number
            for index, number in enumerate(number_values)
        },
    ))


@app.route("/expediente")
@require()
def record():
    target=request.args.get("student_id",type=int)
    uid=target if session["role"]=="teacher" else session["user_id"]
    if not uid: return redirect(url_for("students"))
    with connect() as c:
        student=c.execute("SELECT * FROM users WHERE id=? AND role='student'",(uid,)).fetchone()
        if not student: abort(404)
        if session["role"] == "student":
            session["group_name"] = student["group_name"]
            session["semester"] = student["semester"]
        rows=c.execute(
            """SELECT a.*,g.id grade_id,g.grade,g.status,g.comments,g.submitted_at
               FROM activities a
               LEFT JOIN grades g ON g.activity_id=a.id AND g.student_id=?
               WHERE (a.group_name=? AND a.semester=?) OR g.id IS NOT NULL
               ORDER BY a.partial,a.id""",
            (uid,student["group_name"],student["semester"]),
        ).fetchall()
    page_size = 8
    pages = []
    for start in range(0, len(rows), page_size):
        page_rows = rows[start:start + page_size]
        pages.append({
            "rows": page_rows,
            "has_pending_evidence": any(
                row["grade_id"] and row["status"] != "Revisada" for row in page_rows
            ),
        })
    if not pages:
        pages.append({"rows": [], "has_pending_evidence": False})
    return render_template(
        "record.html",
        student=student,
        pages=pages,
    )

@app.route("/exportar/alumnos.csv")
@require("teacher")
def export_students():
    with connect() as c:
        rows = c.execute(
            """SELECT full_name,first_name,last_name,email,semester,group_name,list_number,username,active
               FROM users WHERE role='student' AND approved=1
               ORDER BY LOWER(group_name),semester NULLS LAST,list_number NULLS LAST,
                        LOWER(COALESCE(NULLIF(last_name,''),full_name)),
                        LOWER(COALESCE(NULLIF(first_name,''),full_name)),
                        id"""
        ).fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Nombre completo","Nombre(s)","Apellidos","Correo","Semestre","Grupo","Numero de lista","Usuario","Estado"])
    for r in rows:
        w.writerow([
            _spreadsheet_safe(r["full_name"]), _spreadsheet_safe(r["first_name"]),
            _spreadsheet_safe(r["last_name"]), _spreadsheet_safe(r["email"]),
            r["semester"], _spreadsheet_safe(r["group_name"]), r["list_number"],
            _spreadsheet_safe(r["username"]), "Activo" if r["active"] else "Desactivado",
        ])
    res=make_response("\ufeff"+out.getvalue()); res.headers["Content-Disposition"]="attachment; filename=alumnos_cecytem.csv"; res.headers["Content-Type"]="text/csv; charset=utf-8"; return res


def _spreadsheet_safe(value):
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _performance_report(c, group_filter="", semester_filter="", student_search=""):
    if not group_filter or not semester_filter:
        return [], []
    cohort_clause = " AND u.group_name=? AND u.semester=?"
    cohort_args = (group_filter, int(semester_filter))
    search_clause = (
        " AND (LOWER(u.full_name) LIKE LOWER(?) OR CAST(u.list_number AS TEXT) LIKE ?)"
        if student_search else ""
    )
    search_args = (f"%{student_search}%", f"%{student_search}%") if student_search else ()
    student_args = cohort_args + search_args
    groups = c.execute(
        """SELECT u.group_name,u.semester,COUNT(DISTINCT u.id) students,
                  COUNT(g.id) assignments,
                  SUM(CASE WHEN g.status IN ('Pendiente','Entregada') THEN 1 ELSE 0 END) pending,
                  SUM(CASE WHEN g.status='Revisada' THEN 1 ELSE 0 END) reviewed,
                  ROUND(AVG(g.grade),2) average
           FROM users u
           LEFT JOIN grades g ON g.student_id=u.id AND g.activity_id IN
             (SELECT id FROM activities
              WHERE group_name=u.group_name AND semester=u.semester)
           WHERE u.role='student' AND u.approved=1 AND u.active=1"""
           + cohort_clause
           + search_clause
           + " GROUP BY u.group_name,u.semester ORDER BY LOWER(u.group_name),u.semester",
           student_args,
    ).fetchall()
    students = c.execute(
        """SELECT u.id,u.full_name,u.group_name,u.list_number,u.semester,
                  COUNT(g.id) assignments,
                  SUM(CASE WHEN g.status IN ('Pendiente','Entregada') THEN 1 ELSE 0 END) pending,
                  SUM(CASE WHEN g.status='Revisada' THEN 1 ELSE 0 END) reviewed,
                  ROUND(AVG(g.grade),2) average
           FROM users u
           LEFT JOIN grades g ON g.student_id=u.id AND g.activity_id IN
             (SELECT id FROM activities
              WHERE group_name=u.group_name AND semester=u.semester)
           WHERE u.role='student' AND u.approved=1 AND u.active=1"""
        + cohort_clause
           + search_clause
           + """ GROUP BY u.id
             ORDER BY LOWER(u.group_name),u.semester NULLS LAST,u.list_number NULLS LAST,
                      LOWER(COALESCE(NULLIF(u.last_name,''),u.full_name)),
                      LOWER(COALESCE(NULLIF(u.first_name,''),u.full_name)),
                      u.id""",
        student_args,
    ).fetchall()
    return groups, students


@app.route("/reportes")
@require("teacher")
def reports():
    selected_group = request.args.get("group", "").strip().upper()
    selected_semester = request.args.get("semester", "").strip()
    if selected_semester not in {"1", "2", "3", "4", "5", "6"}:
        selected_semester = ""
    search = request.args.get("q", "").strip()
    with connect() as c:
        groups, students = _performance_report(
            c, selected_group, selected_semester, search
        )
        available_groups = c.execute(
            """SELECT group_name FROM users
               WHERE role='student' AND approved=1 AND active=1
               GROUP BY group_name
               ORDER BY LOWER(group_name)"""
        ).fetchall()
        semester_options = c.execute(
            """SELECT DISTINCT semester FROM users
               WHERE role='student' AND approved=1 AND active=1 AND semester IS NOT NULL
               ORDER BY semester"""
        ).fetchall()
    return render_template(
        "reports.html", groups=groups, students=students,
        available_groups=available_groups, semester_options=semester_options,
        selected_group=selected_group, selected_semester=selected_semester, q=search,
        generated_on=date.today().strftime("%d/%m/%Y"),
    )


@app.route("/exportar/reporte.csv")
@require("teacher")
def export_report():
    selected_group = request.args.get("group", "").strip().upper()
    selected_semester = request.args.get("semester", "").strip()
    if selected_semester not in {"1", "2", "3", "4", "5", "6"}:
        selected_semester = ""
    search = request.args.get("q", "").strip()
    with connect() as c:
        _, students = _performance_report(
            c, selected_group, selected_semester, search
        )
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "Grupo", "Numero de lista", "Alumno", "Semestre",
        "Actividades asignadas", "Pendientes", "Revisadas", "Promedio",
    ])
    for student in students:
        writer.writerow([
            _spreadsheet_safe(student["group_name"]), student["list_number"],
            _spreadsheet_safe(student["full_name"]),
            student["semester"] or "", student["assignments"], student["pending"] or 0,
            student["reviewed"] or 0, student["average"] if student["average"] is not None else "",
        ])
    response = make_response("\ufeff" + out.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=reporte_desempeno_cecytem.csv"
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    return response

@app.route("/perfil")
@require()
def profile():
    with connect() as c: user=c.execute("SELECT * FROM users WHERE id=?",(session["user_id"],)).fetchone()
    return render_template("profile.html",user=user)

@app.errorhandler(403)
def forbidden(_): return render_template("message.html",title="Acceso restringido",message="Tu cuenta no tiene permisos para esta sección."),403

if __name__=="__main__":
    init_db()
    debug_enabled = os.environ.get("FLASK_DEBUG", "").lower() == "true"
    app.run(debug=debug_enabled and not os.environ.get("RENDER"))


def create_app():
    init_db()
    return app
