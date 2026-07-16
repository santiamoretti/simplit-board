# simplit-board

**El agente que convierte una computadora común en un "board" (equipo) de SimplitSecurity.**

`simplit-board` es un programa chiquito que corre en una terminal. Hace tres cosas: le da al equipo una
identidad, lo registra en la nube de SimplitSecurity, y se queda escuchando para recibir el software de
seguridad (firmado) que un operador le envía desde la consola. El agente **no** hace el trabajo de seguridad
él mismo: solo se ocupa de la identidad y del canal de actualización. El software de seguridad de verdad (que
está hecho en Java) llega después, cuando alguien lo "empuja" desde la consola web.

> Pensalo como el **portero de un edificio**: no hace el trabajo de seguridad, pero se identifica, le abre la
> puerta solo al software autorizado (verificando que la firma sea legítima), lo instala y lo deja trabajando.

---

## 1. ¿Qué necesitás antes de empezar?

- Una computadora con **Python 3.9 o más nuevo**. Para ver qué versión tenés:
  ```bash
  python3 --version
  ```
- **Conexión a internet.**
- Una **cuenta de operador** de SimplitSecurity (email y contraseña) que tenga permiso para dar de alta
  equipos (el permiso se llama `enrollDevice`). Si no la tenés, pedísela a quien administra tu organización.

No hace falta instalar Java ni nada más a mano. El software de seguridad se descarga y se instala **solo**
cuando te lo envían.

---

## 2. Instalación

Te vamos a dar un archivo que termina en `.whl` (es el paquete de Python, listo para instalar). Guardalo en
una carpeta, abrí una terminal **parado en esa carpeta**, y corré:

```bash
pip install simplit_board-0.1.0-py3-none-any.whl
```

Eso instala el comando `simplit-board`. Para comprobar que quedó bien instalado:

```bash
simplit-board --help
```

Si aparece una lista de comandos (`register`, `up`, `status`, `bootstrap`), listo. Ya está.

> 💡 **Consejo:** si compartís la computadora o querés mantener todo ordenado, podés crear un "entorno
> virtual" antes de instalar (opcional):
> ```bash
> python3 -m venv venv
> source venv/bin/activate     # en Windows: venv\Scripts\activate
> pip install simplit_board-0.1.0-py3-none-any.whl
> ```

---

## 3. Cómo usarlo (3 pasos)

### Paso 1 — Registrar el equipo

```bash
simplit-board register
```

Qué va a pasar, en orden:

1. El equipo se inventa un **nombre lindo** (por ejemplo `psychedelic-turaco-1095`) y genera su identidad
   criptográfica **una sola vez**. Queda guardada, así que si reiniciás la máquina sigue siendo el mismo
   equipo (no se duplica).
2. Te va a pedir tu **email** y tu **contraseña** de operador.
3. Te va a preguntar **dónde querés ubicar el equipo**: en la raíz de la organización, o dentro de una de tus
   subdivisiones (por ejemplo `Buenos Aires HQ`). Elegís escribiendo un número de la lista.
4. Listo: el equipo queda dado de alta en el lugar que elegiste.

Si preferís no responder preguntas (por ejemplo, para automatizar), podés pasar todo de una vez:

```bash
simplit-board register --email vos@ejemplo.com --password 'tu-contraseña' --subdivision "Buenos Aires HQ"
```

> ⚠️ Tu cuenta tiene que tener permiso para dar de alta equipos **y** para crear en el lugar que elegís. Si no,
> vas a ver un error de "no autorizado" (`not authorized`). Eso lo decide el motor de permisos de
> SimplitSecurity, no este programa: pedile a tu administrador que te dé el permiso.

> 🔐 **Si tu cuenta tiene verificación en dos pasos (2FA):** después del email y la contraseña, el programa te
> va a pedir el **código de 6 dígitos** de tu app de autenticación (Google Authenticator, Authy, 1Password, …).
> Si es la **primera vez**, primero te muestra un **código secreto** para agregar a la app, y después te pide
> el código que la app genera (y te da unos **códigos de respaldo** para guardar). Podés pasar el código de una
> con `--mfa-code 123456`, pero como cambia cada 30 segundos, normalmente conviene dejar que te lo pregunte.
>
> Nota: si tu cuenta además tiene que **cambiar la contraseña** por primera vez, hacelo una vez en la web
> (`iniciá sesión en la consola`) y después volvé a correr `register`.

### Paso 2 — Ponerlo en línea y esperar

```bash
simplit-board up
```

Qué va a pasar:

- El equipo se conecta a la nube y se queda **esperando**. Vas a ver algo así:
  ```
  board 'psychedelic-turaco-1095' is online — no software installed yet.
  [presence] connected — waiting for pushes
  ```
- **Dejá esta ventana abierta.** El equipo está escuchando.
- Un operador, desde la consola web (pestaña **Updates → Stream to board**), le envía el software de
  seguridad. El agente **verifica la firma**, lo instala solo (con lo que recibió, sin descargar nada por su
  cuenta) y le pasa el control al software.

A partir de ahí el equipo se maneja **solo**: se reinicia con la versión nueva y, cuando le mandan otra
actualización más adelante, se actualiza a sí mismo sin que tengas que hacer nada.

### Paso 3 — Ver el estado (opcional)

```bash
simplit-board status
```

Te muestra la identidad del equipo y qué versión del software tiene instalada.

---

## 4. Configuración (opcional)

El programa ya viene apuntando a la nube de SimplitSecurity. **No necesitás tocar nada** para el uso normal.
Solo cambiá estas variables de entorno si sabés lo que estás haciendo:

| Variable | Valor por defecto | Para qué sirve |
|---|---|---|
| `SIMPLIT_STATE_DIR` | `/var/lib/simplit` | Dónde se guardan la identidad y la credencial del equipo |
| `SIMPLIT_DOMAIN` | la nube en vivo | El dominio de los servicios en la nube |
| `SIMPLIT_ORG` | `simplit` | La organización |

La identidad se genera una sola vez y se guarda de forma segura, así que un corte de luz nunca crea un equipo
nuevo por accidente.

---

## 5. Problemas comunes

- **Al hacer `up` aparece `CERTIFICATE_VERIFY_FAILED`** (pasa sobre todo en Mac con un entorno virtual recién
  creado): tu Python no encuentra los certificados de seguridad. Solución — corré esto y volvé a intentar:
  ```bash
  export SSL_CERT_FILE="$(python -c 'import certifi; print(certifi.where())')"
  export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
  simplit-board up
  ```
  (En un equipo Linux de verdad esto no suele pasar.)

- **Al registrar aparece `not authorized to enrol devices`**: tu cuenta no tiene el permiso `enrollDevice`, o
  no podés crear en la subdivisión que elegiste. Pedile a tu administrador que te dé el permiso.

- **`register` te vuelve a pedir email y contraseña**: es normal solo la primera vez. Una vez registrado, la
  credencial queda guardada y no te la vuelve a pedir.

- **El comando `simplit-board` no existe** después de instalar: cerrá y volvé a abrir la terminal, o revisá
  que hayas activado el entorno virtual donde lo instalaste (`source venv/bin/activate`).

---

## 6. ¿Cómo funciona por dentro? (resumen simple)

1. **`register`** — el equipo genera su identidad, vos iniciás sesión como operador, y el equipo queda dado de
   alta en el lugar que elegiste. Todo lo autoriza el motor de permisos.
2. **`up`** — el equipo toma un canal cifrado y espera. Cuando le mandan el software (firmado), lo verifica, lo
   instala desde lo que recibió (sin descargar nada por su cuenta) y le cede el control.
3. **A partir de ahí** — el software de seguridad (en Java) maneja el equipo: hace escaneos, guarda los
   resultados y responde consultas, siempre por un canal cifrado de punta a punta. Nadie en el medio (ni la
   nube) puede ver el contenido.

Requisitos técnicos: Python 3.9+. Depende de `click`, `requests`, `cryptography`, `websocket-client` y
`coolname` (se instalan solas con el paquete).
