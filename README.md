# EduControl CECyTEM

Portal escolar desarrollado con Flask. En local utiliza SQLite; en Render debe conectarse a PostgreSQL mediante `DATABASE_URL` (por ejemplo, una base de Neon). La base de datos se inicializa y migra de forma aditiva al arrancar la aplicación.

## Desarrollo local

1. Instala Python 3.12.8 o una versión compatible con `requirements.txt`.
2. Desde la carpeta que contiene `app.py`, crea y activa un entorno virtual:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Inicia la aplicación:

   ```powershell
   python app.py
   ```

4. Abre <http://127.0.0.1:5000>.

La aplicación local crea `educ_control.db` junto a `app.py`. Ese archivo está excluido de Git. El usuario docente inicial es `docente`; en una base vacía, establece `INITIAL_TEACHER_PASSWORD` antes del primer arranque con una contraseña única de al menos 12 caracteres. En PowerShell puedes capturarla sin mostrarla:

```powershell
$securePassword = Read-Host "Contraseña inicial docente" -AsSecureString
$env:INITIAL_TEACHER_PASSWORD = [System.Net.NetworkCredential]::new("", $securePassword).Password
python app.py
Remove-Item Env:INITIAL_TEACHER_PASSWORD
```

En una base existente no necesitas volver a configurarla. Sin `SECRET_KEY`, el modo local genera una clave aleatoria al iniciar; las sesiones activas se invalidan al reiniciar. En Render ambas variables deben configurarse como secretos.

## Variables de entorno

| Variable | Requerida | Uso |
|---|---:|---|
| `DATABASE_URL` | Sí en Render | URL PostgreSQL copiada desde el panel de Neon. Usa el endpoint pooled y `sslmode=require`. |
| `SECRET_KEY` | Sí en Render | Firma segura de las sesiones Flask. Render la genera mediante `render.yaml`. |
| `INITIAL_TEACHER_PASSWORD` | Sí al crear por primera vez la cuenta docente | Contraseña inicial única de 12 caracteres o más. No se guarda en Git. Cambiar esta variable después de crear la cuenta no cambia la contraseña existente. |
| `FLASK_DEBUG` | No | Solo para desarrollo local. La depuración nunca se activa en Render. |

`\.env.example` contiene únicamente valores de muestra. Nunca lo reemplaces con credenciales reales ni publiques un archivo `.env`. El `.gitignore` excluye bases locales, `.env` y entornos virtuales.

## Publicar en GitHub

1. Este repositorio es público. No agregues contraseñas, archivos `.env`, `educ_control.db`, volcados de base de datos ni capturas con información personal. Si necesitas almacenar información escolar privada, utiliza un repositorio privado.
2. Sube **el contenido de esta carpeta**, incluyendo `app.py`, `templates`, `static`, `requirements.txt`, `Procfile`, `render.yaml` y `.python-version`, a la raíz del repositorio. No es necesario subir `.venv`.
3. Si lo haces desde una terminal y aún no inicializaste Git:

   ```powershell
   git init
   git branch -M main
   git add .
   git status --short
   ```

   Revisa la lista cuidadosamente: no debe mostrar `.env`, `educ_control.db`, `.venv` ni secretos. Luego:

   ```powershell
   git commit -m "Prepare EduControl for Render and Neon"
   git remote add origin https://github.com/TU-USUARIO/TU-REPOSITORIO.git
   git push -u origin main
   ```

   Si el repositorio ya existe, conserva su configuración y agrega el remoto solo si todavía no está definido. No subas la base de datos SQLite: contiene registros locales y no es una base compartida ni persistente en Render.

## Crear Neon y desplegar en Render

1. En Neon, crea un proyecto PostgreSQL y elige una región cercana a Render y a la comunidad escolar. En **Connect**, selecciona la base, el rol y la opción **Connection pooling**. Copia la URL PostgreSQL completa; debe incluir SSL, normalmente `sslmode=require`. Trátala como contraseña.
2. En Render, selecciona **New → Blueprint** y conecta el repositorio de GitHub. Render encontrará `render.yaml`, que configura un servicio web gratuito, la comprobación `/healthz`, Gunicorn y las variables de entorno.
3. Completa los valores solicitados para `DATABASE_URL` y `INITIAL_TEACHER_PASSWORD`. El Blueprint genera `SECRET_KEY`. La contraseña docente debe ser única, larga y guardarse en un gestor de contraseñas. No la pongas en el YAML, README, GitHub ni en capturas.
4. Render instala `requirements.txt` y ejecuta:

   ```text
   gunicorn --workers 1 --threads 4 --timeout 120 "app:create_app()"
   ```

   La fábrica inicializa el esquema PostgreSQL idempotentemente antes de atender tráfico. La ruta `/healthz` comprueba también que la base esté disponible. La cookie de sesión se marca segura en Render.
5. Espera a que el despliegue indique **Live** y revisa los logs. Abre la URL HTTPS `https://...onrender.com`, entra con usuario `docente` y la contraseña inicial configurada, y registra una cuenta de prueba. Confirma su aprobación docente, crea una actividad y una calificación, reinicia o vuelve a desplegar el servicio y verifica que esos datos sigan presentes.
6. Render puede desplegar automáticamente los siguientes cambios que subas a la rama conectada. Revisa cada `git status` antes de `git add` y nunca incluyas secretos.

## Migrar registros existentes de SQLite a Neon

La SQLite local **no se copia automáticamente** al desplegar. Si `educ_control.db` ya contiene información que se debe conservar:

1. Despliega primero la aplicación y configura Neon. Antes de migrar, deja la base Neon recién creada: solo puede tener la cuenta inicial `docente`; la herramienta se niega a mezclar datos con tablas que ya tengan registros.
2. En tu equipo, activa el entorno virtual y configura localmente `DATABASE_URL` con la URL Neon y `INITIAL_TEACHER_PASSWORD`. No pegues la URL en GitHub ni en este archivo.
3. Ejecuta desde esta carpeta:

   ```powershell
   python migrate_sqlite_to_postgres.py
   ```

   Para una copia SQLite en otra ubicación, pasa la ruta como argumento. El proceso conserva IDs y hashes existentes, importa las tablas disponibles, ajusta las secuencias y verifica los conteos; no borra ni modifica el archivo fuente. Si la migración falla, revisa el mensaje y los logs antes de reintentar.
4. Si SQLite ya tenía la cuenta `docente`, la contraseña que se conserva es la de esa cuenta local; `INITIAL_TEACHER_PASSWORD` solo crea la cuenta inicial de una base vacía. Tras confirmar que el portal muestra los registros esperados en Render, protege la URL de Neon y los respaldos como información confidencial. Evita migrar dos veces sobre una base que ya tenga registros.

## Persistencia, límites y respaldos

- Los datos de producción viven en PostgreSQL, no en el disco del servicio web. El almacenamiento local gratuito de Render es efímero: no uses SQLite en producción ni guardes allí información que deba sobrevivir reinicios.
- La aplicación usa una sola instancia y conexiones por solicitud; Gunicorn corre con un worker y cuatro hilos para un uso escolar pequeño. Neon recibe la conexión pooled recomendada y cifra el transporte con SSL.
- Los servicios gratuitos de Render pueden suspenderse tras inactividad y tardar en despertar. Las cuotas y políticas de los planes gratuitos de Neon pueden cambiar; compruébalas en sus paneles. Un plan gratuito es adecuado para pruebas y uso escolar ligero, pero no ofrece las garantías de disponibilidad, soporte y recuperación de un servicio de pago. Configura y prueba exportaciones/respaldos antes de guardar registros oficiales.

## Comprobaciones de interfaz

La interfaz utiliza HTML adaptable y hojas de estilo móviles. Después de desplegar, verifica en computadora y en un teléfono (o emulación móvil) el acceso, el menú lateral, registro, actividades, calificaciones, calendario y notificaciones. Comprueba que no haya desbordamiento horizontal y que el HTTPS cargue correctamente. El endpoint de salud no reemplaza estas comprobaciones funcionales.

Cada cuenta puede elegir desde **Configuración** sus colores y apariencia, tamaño de texto y botones, secciones visibles en Inicio, avisos que quiere mostrar, accesos favoritos, vista inicial del calendario, reducción de animaciones y si Cecyto aparece en las páginas. El Inicio de los alumnos también puede mostrar sus próximas entregas. Estas preferencias se guardan por usuario en la base de datos y se aplican al iniciar sesión desde otro dispositivo; no alteran las preferencias de otras cuentas.

Las páginas se adaptan a celulares: los formularios y botones se acomodan a pantallas pequeñas, y las tablas pueden desplazarse horizontalmente sin ensanchar toda la página.
