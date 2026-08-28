# Plataforma de despliegue BigQuery

Esta solución usa **dos repositorios separados**. Esta separación permite que el usuario gestione únicamente artefactos BigQuery y que DevOps conserve el pipeline, las credenciales, los proyectos GCP y las reglas de validación.

```text
Repositorio central de DevOps                 Repositorio de artefactos del usuario
----------------------------------            ------------------------------------
.github/workflows/                             tbl/<dataset>/*.json
scripts/                                       sp/<dataset>/*.sql
schemas/
tests/
requirements.txt
```

El repositorio del usuario no necesita workflows, README, configuración ni credenciales. El despliegue central lo descarga y rechaza cualquier archivo versionado que no corresponda a uno de estos dos patrones:

```text
tbl/<dataset>/<tabla>.json
sp/<dataset>/<NNN>_<procedimiento>.sql
```

La carpeta `.git` interna queda fuera de esta regla. Git no permite versionar carpetas vacías, por lo que un repositorio de usuario nuevo queda vacío hasta que se suba el primer JSON o SQL.

## 1. Subir este repositorio central

Este directorio completo corresponde al repositorio privado administrado por DevOps. Para publicarlo por primera vez:

```bash
cd /Users/dbilbao/Github/bigquery-deploy
git init -b main
git add .
git commit -m "Initial BigQuery deployment platform"
gh repo create TU_OWNER/bigquery-deploy-platform --private --source=. --remote=origin --push
```

Reemplace `TU_OWNER` por el usuario u organización de GitHub. También puede crear primero el repositorio vacío desde GitHub y ejecutar:

```bash
git remote add origin git@github.com:TU_OWNER/bigquery-deploy-platform.git
git push -u origin main
```

No ejecute estos comandos desde un repositorio de artefactos de usuario.

## 2. Configurar el repositorio central

En `Settings > Secrets and variables > Actions`, configure lo siguiente.

### Secret para leer repositorios de usuarios

```text
ARTIFACT_REPOSITORY_TOKEN
```

Para la prueba puede ser un fine-grained personal access token con acceso de solo lectura a `Contents` en los repositorios de artefactos. Más adelante conviene reemplazarlo por una GitHub App instalada solamente en los repositorios administrados.

### Secret opcional para crear repositorios

```text
REPOSITORY_FACTORY_TOKEN
```

Se usa únicamente en el workflow `Create BigQuery artifact repository`. El token debe poder crear repositorios para el usuario u organización elegidos. El workflow crea el repositorio vacío: no agrega README, `.gitignore`, workflows ni archivos auxiliares.

### Variables por ambiente

| Ambiente | Variables del repositorio central |
|---|---|
| DEV | `BQ_PROJECT_ID_DEV`, `BQ_LOCATION_DEV`, `GCP_WIF_PROVIDER_DEV`, `GCP_SERVICE_ACCOUNT_DEV`, `BQ_POLICY_TAGS_DEV` |
| QA | `BQ_PROJECT_ID_QA`, `BQ_LOCATION_QA`, `GCP_WIF_PROVIDER_QA`, `GCP_SERVICE_ACCOUNT_QA`, `BQ_POLICY_TAGS_QA` |
| PROD | `BQ_PROJECT_ID_PROD`, `BQ_LOCATION_PROD`, `GCP_WIF_PROVIDER_PROD`, `GCP_SERVICE_ACCOUNT_PROD`, `BQ_POLICY_TAGS_PROD` |

Ejemplo para desarrollo:

```text
BQ_PROJECT_ID_DEV=my-bigquery-project-dev
BQ_LOCATION_DEV=southamerica-west1
GCP_WIF_PROVIDER_DEV=projects/123456789/locations/global/workloadIdentityPools/github/providers/github
GCP_SERVICE_ACCOUNT_DEV=bq-deployer@my-bigquery-project-dev.iam.gserviceaccount.com
```

Todas estas variables permanecen en el repositorio central. El repositorio del usuario no contiene proyectos GCP, regiones, tokens ni nombres de service accounts.

## 3. Policy tags

Cada ambiente define su propio mapa en `BQ_POLICY_TAGS_DEV`, `BQ_POLICY_TAGS_QA` o `BQ_POLICY_TAGS_PROD`. El valor es un objeto JSON:

```json
{
  "customer_data.email": "projects/111111111111/locations/southamerica-west1/taxonomies/1001/policyTags/2001"
}
```

El usuario utiliza únicamente el alias `taxonomy_name.policytag_name` dentro del manifiesto:

```json
{
  "name": "email",
  "type": "STRING",
  "policyTag": "customer_data.email"
}
```

El pipeline valida que el alias exista y que la ubicación del resource name coincida con la del ambiente.

## 4. Crear un repositorio de artefactos

Una vez publicado el repositorio central, abra `Actions > Create BigQuery artifact repository > Run workflow` e indique:

- propietario tipo `user` u `organization`;
- usuario u organización;
- nombre del repositorio;
- visibilidad `private` o `public`.

Para una primera prueba también puede crear manualmente un repositorio vacío en GitHub, siempre sin inicializarlo con README, `.gitignore` o licencia.

## 5. Qué debe subir el usuario

Ejemplo de repositorio válido:

```text
tbl/
  core/
    clientes.json
sp/
  core/
    010_sp_upsert_cliente.sql
    020_sp_recalcular_saldos.sql
```

Puede contener solo tablas, solo procedimientos o ambos. No es obligatorio que estén presentes las dos carpetas.

Para el primer commit de prueba:

```bash
git clone git@github.com:TU_OWNER/REPOSITORIO_ARTEFACTOS.git
cd REPOSITORIO_ARTEFACTOS
mkdir -p tbl/core sp/core
cp /Users/dbilbao/Github/bigquery-deploy/tbl/core/clientes.json tbl/core/
cp /Users/dbilbao/Github/bigquery-deploy/sp/core/010_sp_upsert_cliente.sql sp/core/
git add tbl sp
git commit -m "Add BigQuery artifacts"
git push -u origin main
```

Para cambios posteriores, el usuario modifica o agrega solamente archivos dentro de `tbl/` y `sp/` y realiza un nuevo commit.

## 6. Evidenciar el dataset en cada archivo

Una tabla declara el dataset tanto en la ruta como en `metadata.dataset`; ambos valores deben coincidir:

```text
tbl/core/clientes.json
```

```json
{
  "apiVersion": "bqdeploy/v1",
  "kind": "Table",
  "metadata": {
    "dataset": "core",
    "name": "clientes",
    "description": "Maestro de clientes."
  },
  "spec": {
    "schema": {
      "fields": [
        {
          "name": "cliente_id",
          "type": "STRING",
          "mode": "REQUIRED",
          "maxLength": "100",
          "description": "Identificador del cliente."
        },
        {
          "name": "limite_credito",
          "type": "NUMERIC",
          "mode": "NULLABLE",
          "precision": "15",
          "scale": "6"
        },
        {
          "name": "atributos",
          "type": "JSON",
          "mode": "NULLABLE"
        },
        {
          "name": "direccion",
          "type": "STRUCT",
          "mode": "NULLABLE",
          "fields": [
            {
              "name": "calle",
              "type": "STRING",
              "maxLength": "150"
            }
          ]
        }
      ]
    }
  }
}
```

Un procedimiento declara el dataset en la ruta y en el identificador del `CREATE OR REPLACE`; también deben coincidir:

```text
sp/core/010_sp_upsert_cliente.sql
```

```sql
CREATE OR REPLACE PROCEDURE `core.sp_upsert_cliente`(
  IN p_cliente_id STRING
)
OPTIONS (
  description = 'Crea o actualiza un cliente.'
)
BEGIN
  SELECT p_cliente_id;
END;
```

El SQL no debe incluir un proyecto fijo como `mi-proyecto.core.sp_upsert_cliente`: el proyecto se selecciona en el pipeline según el ambiente. Los prefijos `010_`, `020_`, etc. determinan el orden de despliegue.

## 7. Ejecutar un despliegue

Desde el repositorio central, abra `Actions > Deploy BigQuery artifacts > Run workflow` y complete:

- `source_repository`: `owner/repositorio-artefactos`;
- `source_ref`: branch, tag o SHA, por ejemplo `main`;
- `target_environment`: `dev`, `qa` o `prod`;
- `operation`: primero `plan`; después `apply`.

El workflow realiza este flujo:

1. Descarga el motor central y el repositorio de artefactos por separado.
2. Rechaza archivos distintos de `tbl/<dataset>/*.json` y `sp/<dataset>/*.sql`.
3. Valida nombres, datasets, JSON Schema, tipos, parámetros, policy tags y SQL.
4. Autentica mediante Workload Identity Federation.
5. Comprueba que cada dataset exista y que su ubicación coincida con `BQ_LOCATION_<AMBIENTE>`.
6. Construye el plan completo antes de modificar BigQuery.
7. En `apply`, crea o actualiza tablas y ejecuta `CREATE OR REPLACE PROCEDURE` en orden.
8. Publica `deployment-report.json` como artefacto de la ejecución.

Esta versión de prueba se ejecuta manualmente. Las aprobaciones y la automatización por eventos pueden agregarse después sin colocar workflows en los repositorios de usuarios.

## Tipos de datos y actualizaciones

El manifiesto admite `STRING`, `BYTES`, `INT64`/`INTEGER`, `FLOAT64`/`FLOAT`, `BOOL`/`BOOLEAN`, `NUMERIC`, `BIGNUMERIC`, `DATE`, `TIME`, `DATETIME`, `TIMESTAMP`, `GEOGRAPHY`, `JSON`, `INTERVAL`, `RANGE` y `STRUCT`/`RECORD`. Los arreglos usan `mode: REPEATED`; un arreglo de estructuras usa `type: STRUCT` y `mode: REPEATED`.

Se aplican de forma segura la creación de tablas, descripciones, labels, policy tags, columnas nuevas compatibles, relajación de `REQUIRED` a `NULLABLE`, ampliaciones parametrizadas soportadas, clustering, opciones de particionamiento y creación o reemplazo de procedimientos.

Se bloquean antes de aplicar eliminaciones o reordenamientos de columnas, columnas `REQUIRED` nuevas en tablas existentes, reducciones de longitud/precisión/escala, conversiones incompatibles, cambios no soportados en campos anidados y cambios del campo o estrategia de particionamiento. Eliminar un archivo no elimina el recurso existente en BigQuery.

## Requisitos de GCP e IAM

Los datasets deben existir previamente. Deben estar habilitadas BigQuery API y, si se utilizan policy tags, Data Catalog API.

Como base, cada service account requiere:

- `roles/bigquery.jobUser` en el proyecto;
- `roles/bigquery.dataOwner` en los datasets administrados, o un rol personalizado equivalente;
- acceso para consultar taxonomías y asociar policy tags;
- `roles/iam.workloadIdentityUser` para la identidad federada de GitHub.

No se almacena una llave JSON de service account.

## Validación local del motor central

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --requirement requirements.txt
python -m unittest discover -s tests -v
python scripts/bq_deploy.py validate --root .
```

Los códigos de salida son: `0` éxito, `2` artefacto inválido, `3` error de preflight, `4` cambio destructivo o no soportado, `5` fallo al aplicar y `10` error inesperado.
