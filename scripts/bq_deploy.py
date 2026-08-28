#!/usr/bin/env python3
"""Validate and reconcile declarative BigQuery tables and procedures."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import sys
import traceback
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

EXIT_VALIDATION = 2
EXIT_PREFLIGHT = 3
EXIT_BLOCKED = 4
EXIT_APPLY = 5
EXIT_UNEXPECTED = 10

TYPE_ALIASES = {
    "INTEGER": "INT64",
    "FLOAT": "FLOAT64",
    "BOOLEAN": "BOOL",
    "RECORD": "STRUCT",
}
CLUSTERABLE_TYPES = {
    "BIGNUMERIC",
    "BOOL",
    "DATE",
    "DATETIME",
    "GEOGRAPHY",
    "INT64",
    "NUMERIC",
    "RANGE",
    "STRING",
    "TIMESTAMP",
}
SAFE_TYPE_WIDENINGS = {
    ("INT64", "NUMERIC"),
    ("INT64", "BIGNUMERIC"),
    ("NUMERIC", "BIGNUMERIC"),
}
PROCEDURE_PATTERN = re.compile(
    r"\bCREATE\s+OR\s+REPLACE\s+PROCEDURE\s+"
    r"(?:`(?P<quoted>[^`]+)`|(?P<plain>[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*))\s*\(",
    re.IGNORECASE,
)
POLICY_RESOURCE_PATTERN = re.compile(
    r"^projects/[^/]+/locations/(?P<location>[^/]+)/taxonomies/[^/]+/policyTags/[^/]+$"
)


class DeploymentFailure(Exception):
    exit_code = EXIT_UNEXPECTED


class ValidationFailure(DeploymentFailure):
    exit_code = EXIT_VALIDATION

    def __init__(self, errors: Iterable[str]):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


class PreflightFailure(DeploymentFailure):
    exit_code = EXIT_PREFLIGHT


class BlockedChanges(DeploymentFailure):
    exit_code = EXIT_BLOCKED


class ApplyFailure(DeploymentFailure):
    exit_code = EXIT_APPLY


@dataclass
class TableArtifact:
    path: Path
    dataset: str
    name: str
    manifest: dict[str, Any]

    @property
    def key(self) -> str:
        return f"{self.dataset}.{self.name}"


@dataclass
class ProcedureArtifact:
    path: Path
    dataset: str
    name: str
    sql: str

    @property
    def key(self) -> str:
        return f"{self.dataset}.{self.name}"


@dataclass
class RepositoryArtifacts:
    tables: list[TableArtifact]
    procedures: list[ProcedureArtifact]


@dataclass
class TablePlan:
    artifact: TableArtifact
    desired_resource: dict[str, Any]
    action: str
    blockers: list[str] = field(default_factory=list)
    ddl_statements: list[str] = field(default_factory=list)
    update_fields: list[str] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        return {
            "resource": self.artifact.key,
            "file": self.artifact.path.as_posix(),
            "action": self.action,
            "updateFields": self.update_fields,
            "ddlStatements": self.ddl_statements,
            "blockers": self.blockers,
        }


@dataclass
class ProcedurePlan:
    artifact: ProcedureArtifact
    action: str

    def report(self) -> dict[str, Any]:
        return {
            "resource": self.artifact.key,
            "file": self.artifact.path.as_posix(),
            "action": self.action,
        }


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def canonical_type(value: str) -> str:
    upper = value.upper()
    return TYPE_ALIASES.get(upper, upper)


def normalize_location(value: str) -> str:
    return value.strip().lower()


def json_path(parts: Iterable[Any]) -> str:
    rendered = "$"
    for part in parts:
        rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
    return rendered


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationFailure(
            [
                f"{path}: JSON inválido en línea {exc.lineno}, columna {exc.colno}: {exc.msg}"
            ]
        ) from exc


def validate_field_semantics(
    field_spec: dict[str, Any], path: str, depth: int, errors: list[str]
) -> None:
    if depth > 15:
        errors.append(
            f"{path}: BigQuery admite como máximo 15 niveles de STRUCT/RECORD"
        )

    field_type = canonical_type(field_spec["type"])
    mode = field_spec.get("mode", "NULLABLE")
    nested = field_spec.get("fields")
    max_length = field_spec.get("maxLength")
    precision = field_spec.get("precision")
    scale = field_spec.get("scale")
    rounding = field_spec.get("roundingMode")
    range_element = field_spec.get("rangeElementType")

    if field_type == "STRUCT":
        if not nested:
            errors.append(f"{path}: STRUCT/RECORD requiere 'fields'")
        if field_spec.get("policyTag"):
            errors.append(f"{path}: los policy tags solo se permiten en campos hoja")
    elif nested is not None:
        errors.append(f"{path}: 'fields' solo se permite para STRUCT/RECORD")

    if field_type == "RANGE" and not range_element:
        errors.append(f"{path}: RANGE requiere 'rangeElementType'")
    if field_type != "RANGE" and range_element is not None:
        errors.append(f"{path}: 'rangeElementType' solo se permite para RANGE")

    if max_length is not None and field_type not in {"STRING", "BYTES"}:
        errors.append(f"{path}: 'maxLength' solo se permite para STRING o BYTES")
    if any(
        value is not None for value in (precision, scale, rounding)
    ) and field_type not in {
        "NUMERIC",
        "BIGNUMERIC",
    }:
        errors.append(
            f"{path}: precision, scale y roundingMode solo se permiten para NUMERIC/BIGNUMERIC"
        )
    if scale is not None and precision is None:
        errors.append(f"{path}: 'scale' requiere 'precision'")

    if precision is not None:
        p = int(precision)
        s = int(scale or 0)
        if field_type == "NUMERIC" and not (0 <= s <= 9 and 1 <= p - s <= 29):
            errors.append(
                f"{path}: NUMERIC requiere 0 <= scale <= 9 y 1 <= precision-scale <= 29"
            )
        if field_type == "BIGNUMERIC" and not (0 <= s <= 38 and 1 <= p - s <= 38):
            errors.append(
                f"{path}: BIGNUMERIC requiere 0 <= scale <= 38 y 1 <= precision-scale <= 38"
            )

    if mode == "REPEATED" and "defaultValueExpression" in field_spec:
        errors.append(f"{path}: un campo REPEATED no puede tener valor predeterminado")

    if nested:
        names: set[str] = set()
        for index, child in enumerate(nested):
            child_name = child["name"].lower()
            child_path = f"{path}.fields[{index}]({child['name']})"
            if child_name in names:
                errors.append(f"{child_path}: nombre de campo duplicado")
            names.add(child_name)
            validate_field_semantics(child, child_path, depth + 1, errors)


def validate_manifest_semantics(manifest: dict[str, Any], path: Path) -> list[str]:
    errors: list[str] = []
    fields = manifest["spec"]["schema"]["fields"]
    names: set[str] = set()
    top_fields: dict[str, dict[str, Any]] = {}

    for index, field_spec in enumerate(fields):
        lowered = field_spec["name"].lower()
        field_path = f"{path}: $.spec.schema.fields[{index}]({field_spec['name']})"
        if lowered in names:
            errors.append(f"{field_path}: nombre de campo duplicado")
        names.add(lowered)
        top_fields[lowered] = field_spec
        validate_field_semantics(field_spec, field_path, 1, errors)

    spec = manifest["spec"]
    time_partition = spec.get("timePartitioning")
    if time_partition:
        field_name = time_partition["field"].lower()
        partition_field = top_fields.get(field_name)
        if not partition_field:
            errors.append(f"{path}: el campo de partición '{field_name}' no existe")
        else:
            partition_type = canonical_type(partition_field["type"])
            if partition_type not in {"DATE", "DATETIME", "TIMESTAMP"}:
                errors.append(
                    f"{path}: partición temporal requiere DATE, DATETIME o TIMESTAMP; "
                    f"'{field_name}' es {partition_type}"
                )
            if partition_field.get("mode", "NULLABLE") == "REPEATED":
                errors.append(f"{path}: el campo de partición no puede ser REPEATED")
            if time_partition["type"] == "HOUR" and partition_type == "DATE":
                errors.append(f"{path}: partición HOUR no admite un campo DATE")

    range_partition = spec.get("rangePartitioning")
    if range_partition:
        field_name = range_partition["field"].lower()
        partition_field = top_fields.get(field_name)
        if not partition_field:
            errors.append(f"{path}: el campo de partición '{field_name}' no existe")
        elif canonical_type(partition_field["type"]) != "INT64":
            errors.append(f"{path}: rangePartitioning requiere un campo INT64")
        elif partition_field.get("mode", "NULLABLE") == "REPEATED":
            errors.append(f"{path}: el campo de partición no puede ser REPEATED")

    clustering = spec.get("clustering", {}).get("fields", [])
    for field_name in clustering:
        cluster_field = top_fields.get(field_name.lower())
        if not cluster_field:
            errors.append(f"{path}: el campo de clustering '{field_name}' no existe")
            continue
        cluster_type = canonical_type(cluster_field["type"])
        if cluster_type not in CLUSTERABLE_TYPES:
            errors.append(
                f"{path}: el campo de clustering '{field_name}' usa el tipo no soportado {cluster_type}"
            )
        if cluster_field.get("mode", "NULLABLE") == "REPEATED":
            errors.append(
                f"{path}: el campo de clustering '{field_name}' no puede ser REPEATED"
            )

    return errors


def parse_procedure(
    path: Path, root: Path, errors: list[str]
) -> ProcedureArtifact | None:
    relative = path.relative_to(root)
    if len(relative.parts) != 3:
        errors.append(
            f"{relative}: ruta inválida; use sp/<dataset>/<NNN>_<procedimiento>.sql"
        )
        return None

    path_dataset = relative.parts[1]
    stem_match = re.fullmatch(r"[0-9]{3}_(?P<name>[A-Za-z_][A-Za-z0-9_]*)", path.stem)
    if not stem_match:
        errors.append(
            f"{relative}: el archivo debe llamarse <NNN>_<procedimiento>.sql, por ejemplo 010_sp_carga.sql"
        )
        return None

    sql = path.read_text(encoding="utf-8")
    matches = list(PROCEDURE_PATTERN.finditer(sql))
    if len(matches) != 1:
        errors.append(
            f"{relative}: debe contener exactamente un CREATE OR REPLACE PROCEDURE dataset.procedimiento"
        )
        return None

    identifier = matches[0].group("quoted") or matches[0].group("plain")
    parts = identifier.split(".")
    if len(parts) != 2:
        errors.append(
            f"{relative}: el procedimiento debe usar dataset.procedimiento sin proyecto hardcodeado"
        )
        return None

    sql_dataset, routine_name = parts
    if sql_dataset != path_dataset:
        errors.append(
            f"{relative}: dataset de la ruta '{path_dataset}' no coincide con el SQL '{sql_dataset}'"
        )
    if routine_name != stem_match.group("name"):
        errors.append(
            f"{relative}: procedimiento del archivo '{stem_match.group('name')}' no coincide con el SQL "
            f"'{routine_name}'"
        )
    if re.search(r"\bDROP\s+PROCEDURE\b", sql, re.IGNORECASE):
        errors.append(
            f"{relative}: DROP PROCEDURE no está permitido en el flujo automático"
        )

    return ProcedureArtifact(
        path=relative, dataset=sql_dataset, name=routine_name, sql=sql
    )


def validate_repository(
    root: Path,
    schema_path: Path | None = None,
    strict_artifact_repository: bool = False,
) -> RepositoryArtifacts:
    errors: list[str] = []
    schema_path = schema_path or root / "schemas" / "table.schema.json"
    if not schema_path.is_file():
        raise ValidationFailure([f"No existe el contrato {schema_path}"])

    schema = load_json(schema_path)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    table_root = root / "tbl"
    procedure_root = root / "sp"
    if not table_root.is_dir():
        errors.append("No existe la carpeta tbl/")
    if not procedure_root.is_dir():
        errors.append("No existe la carpeta sp/")

    if strict_artifact_repository:
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(root)
            if ".git" in relative.parts:
                continue
            valid_table = (
                len(relative.parts) == 3
                and relative.parts[0] == "tbl"
                and candidate.suffix == ".json"
            )
            valid_procedure = (
                len(relative.parts) == 3
                and relative.parts[0] == "sp"
                and candidate.suffix == ".sql"
            )
            if not valid_table and not valid_procedure:
                errors.append(
                    f"{relative}: el repositorio de artefactos solo admite "
                    "tbl/<dataset>/*.json y sp/<dataset>/*.sql"
                )

    invalid_table_files = (
        [p for p in table_root.rglob("*") if p.is_file() and p.suffix != ".json"]
        if table_root.exists()
        else []
    )
    invalid_sp_files = (
        [p for p in procedure_root.rglob("*") if p.is_file() and p.suffix != ".sql"]
        if procedure_root.exists()
        else []
    )
    errors.extend(
        f"{p.relative_to(root)}: solo se permiten archivos .json dentro de tbl/"
        for p in invalid_table_files
    )
    errors.extend(
        f"{p.relative_to(root)}: solo se permiten archivos .sql dentro de sp/"
        for p in invalid_sp_files
    )

    tables: list[TableArtifact] = []
    seen_tables: set[str] = set()
    for path in sorted(table_root.rglob("*.json")) if table_root.exists() else []:
        relative = path.relative_to(root)
        if len(relative.parts) != 3:
            errors.append(f"{relative}: ruta inválida; use tbl/<dataset>/<tabla>.json")
            continue
        try:
            manifest = load_json(path)
        except ValidationFailure as exc:
            errors.extend(exc.errors)
            continue

        schema_errors = sorted(
            validator.iter_errors(manifest), key=lambda item: list(item.path)
        )
        if schema_errors:
            for error in schema_errors:
                errors.append(f"{relative}: {json_path(error.path)}: {error.message}")
            continue

        path_dataset = relative.parts[1]
        metadata = manifest["metadata"]
        if metadata["dataset"] != path_dataset:
            errors.append(
                f"{relative}: metadata.dataset '{metadata['dataset']}' no coincide con la ruta '{path_dataset}'"
            )
        if metadata["name"] != path.stem:
            errors.append(
                f"{relative}: metadata.name '{metadata['name']}' no coincide con el archivo '{path.stem}'"
            )
        errors.extend(validate_manifest_semantics(manifest, relative))

        key = f"{metadata['dataset']}.{metadata['name']}".lower()
        if key in seen_tables:
            errors.append(f"{relative}: tabla duplicada {key}")
        seen_tables.add(key)
        tables.append(
            TableArtifact(
                path=relative,
                dataset=metadata["dataset"],
                name=metadata["name"],
                manifest=manifest,
            )
        )

    procedures: list[ProcedureArtifact] = []
    seen_procedures: set[str] = set()
    for path in (
        sorted(procedure_root.rglob("*.sql")) if procedure_root.exists() else []
    ):
        procedure = parse_procedure(path, root, errors)
        if not procedure:
            continue
        key = procedure.key.lower()
        if key in seen_procedures:
            errors.append(f"{procedure.path}: procedimiento duplicado {key}")
        seen_procedures.add(key)
        procedures.append(procedure)

    if not tables and not procedures:
        errors.append("No se encontraron tablas ni procedimientos para validar")
    if errors:
        raise ValidationFailure(errors)
    return RepositoryArtifacts(tables=tables, procedures=procedures)


def load_policy_tag_mapping(raw: str | None) -> dict[str, str]:
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreflightFailure(
            f"BQ_POLICY_TAGS_JSON no es JSON válido: línea {exc.lineno}, columna {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
    ):
        raise PreflightFailure(
            "BQ_POLICY_TAGS_JSON debe ser un objeto JSON string:string"
        )
    return parsed


def resolve_field_policy_tags(
    field_spec: dict[str, Any],
    mapping: dict[str, str],
    location: str,
    errors: list[str],
    path: str,
) -> dict[str, Any]:
    result = copy.deepcopy(field_spec)
    result["type"] = canonical_type(result["type"])
    if result["type"] == "STRUCT":
        result["type"] = "RECORD"

    alias = result.pop("policyTag", None)
    if alias:
        resource_name = mapping.get(alias)
        if not resource_name:
            errors.append(
                f"{path}: no existe una variable de policy tag para '{alias}'"
            )
        else:
            match = POLICY_RESOURCE_PATTERN.fullmatch(resource_name)
            if not match:
                errors.append(
                    f"{path}: el valor de '{alias}' no es un resource name de policy tag válido"
                )
            elif normalize_location(match.group("location")) != normalize_location(
                location
            ):
                errors.append(
                    f"{path}: policy tag '{alias}' está en '{match.group('location')}', no en '{location}'"
                )
            result["policyTags"] = {"names": [resource_name]}

    if "fields" in result:
        result["fields"] = [
            resolve_field_policy_tags(
                child, mapping, location, errors, f"{path}.{child['name']}"
            )
            for child in result["fields"]
        ]
    return result


def manifest_to_table_resource(
    artifact: TableArtifact, project: str, location: str, mapping: dict[str, str]
) -> dict[str, Any]:
    metadata = artifact.manifest["metadata"]
    spec = artifact.manifest["spec"]
    errors: list[str] = []
    fields = [
        resolve_field_policy_tags(field, mapping, location, errors, field["name"])
        for field in spec["schema"]["fields"]
    ]
    if errors:
        raise PreflightFailure(
            "\n".join(f"{artifact.path}: {error}" for error in errors)
        )

    resource: dict[str, Any] = {
        "tableReference": {
            "projectId": project,
            "datasetId": artifact.dataset,
            "tableId": artifact.name,
        },
        "description": metadata["description"],
        "labels": metadata.get("labels", {}),
        "schema": {"fields": fields},
        "requirePartitionFilter": spec.get("requirePartitionFilter", False),
    }
    if "friendlyName" in metadata:
        resource["friendlyName"] = metadata["friendlyName"]
    if "timePartitioning" in spec:
        resource["timePartitioning"] = spec["timePartitioning"]
    if "rangePartitioning" in spec:
        resource["rangePartitioning"] = spec["rangePartitioning"]
    if "clustering" in spec:
        resource["clustering"] = spec["clustering"]
    return resource


def normalize_field_for_diff(field_spec: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "name": field_spec["name"].lower(),
        "type": canonical_type(field_spec["type"]),
        "mode": field_spec.get("mode", "NULLABLE").upper(),
        "description": field_spec.get("description"),
    }
    for name in (
        "maxLength",
        "precision",
        "scale",
        "roundingMode",
        "collation",
        "defaultValueExpression",
    ):
        value = field_spec.get(name)
        if value is not None:
            normalized[name] = (
                str(value) if name in {"maxLength", "precision", "scale"} else value
            )

    policy_names = field_spec.get("policyTags", {}).get("names", [])
    normalized["policyTags"] = sorted(policy_names)
    if field_spec.get("rangeElementType"):
        normalized["rangeElementType"] = {
            "type": canonical_type(field_spec["rangeElementType"]["type"])
        }
    if field_spec.get("fields"):
        normalized["fields"] = [
            normalize_field_for_diff(child) for child in field_spec["fields"]
        ]
    return normalized


def sql_type(field_spec: dict[str, Any]) -> str:
    field_type = canonical_type(field_spec["type"])
    if field_type in {"STRING", "BYTES"} and field_spec.get("maxLength"):
        return f"{field_type}({field_spec['maxLength']})"
    if field_type in {"NUMERIC", "BIGNUMERIC"} and field_spec.get("precision"):
        if "scale" in field_spec:
            return f"{field_type}({field_spec['precision']},{field_spec['scale']})"
        return f"{field_type}({field_spec['precision']})"
    if field_type == "RANGE":
        return f"RANGE<{field_spec['rangeElementType']['type']}>"
    return field_type


def parameter_change_is_widening(
    current: dict[str, Any], desired: dict[str, Any]
) -> tuple[bool, bool]:
    """Return (changed, safe)."""
    field_type = canonical_type(current["type"])
    if field_type in {"STRING", "BYTES"}:
        old = current.get("maxLength")
        new = desired.get("maxLength")
        if old == new:
            return False, True
        if old is None and new is not None:
            return True, False
        if new is None:
            return True, True
        return True, int(new) >= int(old)

    if field_type in {"NUMERIC", "BIGNUMERIC"}:
        old_p = current.get("precision")
        new_p = desired.get("precision")
        old_s = current.get("scale", "0")
        new_s = desired.get("scale", "0")
        if old_p == new_p and old_s == new_s:
            return False, True
        if old_p is None and new_p is not None:
            return True, False
        if new_p is None:
            return True, True
        old_integer = int(old_p) - int(old_s)
        new_integer = int(new_p) - int(new_s)
        return True, new_integer >= old_integer and int(new_s) >= int(old_s)
    return False, True


def compare_schema_fields(
    current_fields: list[dict[str, Any]],
    desired_fields: list[dict[str, Any]],
    table_fqn: str,
    path: str = "",
) -> tuple[list[str], list[str], bool]:
    blockers: list[str] = []
    ddl_statements: list[str] = []
    current_normalized = [normalize_field_for_diff(item) for item in current_fields]
    desired_normalized = [normalize_field_for_diff(item) for item in desired_fields]
    schema_changed = current_normalized != desired_normalized

    current_map = {item["name"].lower(): item for item in current_fields}
    desired_map = {item["name"].lower(): item for item in desired_fields}
    current_order = [item["name"].lower() for item in current_fields]
    desired_existing_order = [
        item["name"].lower()
        for item in desired_fields
        if item["name"].lower() in current_map
    ]
    if desired_existing_order != current_order:
        blockers.append(
            f"{path or '<schema>'}: no se pueden eliminar, reordenar ni insertar campos entre columnas existentes"
        )

    for name, current in current_map.items():
        desired = desired_map.get(name)
        field_path = f"{path}.{current['name']}" if path else current["name"]
        if desired is None:
            blockers.append(f"{field_path}: eliminar columnas no está permitido")
            continue

        current_type = canonical_type(current["type"])
        desired_type = canonical_type(desired["type"])
        current_mode = current.get("mode", "NULLABLE").upper()
        desired_mode = desired.get("mode", "NULLABLE").upper()

        if current_mode != desired_mode and (current_mode, desired_mode) != (
            "REQUIRED",
            "NULLABLE",
        ):
            blockers.append(
                f"{field_path}: cambio de modo {current_mode} -> {desired_mode} no es seguro"
            )

        if current_type != desired_type:
            target_has_constraints = any(
                desired.get(key) is not None
                for key in ("maxLength", "precision", "scale")
            )
            if (
                not path
                and (current_type, desired_type) in SAFE_TYPE_WIDENINGS
                and not target_has_constraints
            ):
                ddl_statements.append(
                    f"ALTER TABLE `{table_fqn}` ALTER COLUMN `{desired['name']}` SET DATA TYPE {sql_type(desired)}"
                )
            else:
                blockers.append(
                    f"{field_path}: cambio de tipo {current_type} -> {desired_type} requiere migración explícita"
                )
            continue

        parameter_changed, parameter_safe = parameter_change_is_widening(
            current, desired
        )
        if parameter_changed:
            if path:
                blockers.append(
                    f"{field_path}: cambio parametrizado en campo anidado requiere migración explícita"
                )
            elif parameter_safe:
                ddl_statements.append(
                    f"ALTER TABLE `{table_fqn}` ALTER COLUMN `{desired['name']}` SET DATA TYPE {sql_type(desired)}"
                )
            else:
                blockers.append(
                    f"{field_path}: reducir longitud, precisión o escala no está permitido"
                )

        if current_type == "STRUCT":
            nested_blockers, nested_ddl, _ = compare_schema_fields(
                current.get("fields", []),
                desired.get("fields", []),
                table_fqn,
                field_path,
            )
            blockers.extend(nested_blockers)
            ddl_statements.extend(nested_ddl)

    for desired in desired_fields:
        if desired["name"].lower() not in current_map:
            mode = desired.get("mode", "NULLABLE").upper()
            field_path = f"{path}.{desired['name']}" if path else desired["name"]
            if mode not in {"NULLABLE", "REPEATED"}:
                blockers.append(
                    f"{field_path}: una columna nueva en una tabla existente debe ser NULLABLE o REPEATED"
                )

    return blockers, ddl_statements, schema_changed


def partition_identity(
    value: dict[str, Any] | None,
) -> tuple[str | None, str | None] | None:
    if not value:
        return None
    return value.get("type"), value.get("field")


def plan_table(
    client: Any,
    bigquery: Any,
    not_found: type[Exception],
    artifact: TableArtifact,
    resource: dict[str, Any],
) -> TablePlan:
    table_fqn = (
        f"{resource['tableReference']['projectId']}.{artifact.dataset}.{artifact.name}"
    )
    try:
        existing = client.get_table(table_fqn)
    except not_found:
        return TablePlan(artifact=artifact, desired_resource=resource, action="create")

    current = existing.to_api_repr()
    blockers, ddl_statements, schema_changed = compare_schema_fields(
        current.get("schema", {}).get("fields", []),
        resource["schema"]["fields"],
        table_fqn,
    )
    update_fields: list[str] = []

    if current.get("description") != resource.get("description"):
        update_fields.append("description")
    if current.get("friendlyName") != resource.get("friendlyName"):
        update_fields.append("friendly_name")
    if current.get("labels", {}) != resource.get("labels", {}):
        update_fields.append("labels")
    if schema_changed:
        update_fields.append("schema")

    current_time = current.get("timePartitioning")
    desired_time = resource.get("timePartitioning")
    if partition_identity(current_time) != partition_identity(desired_time):
        blockers.append(
            "cambiar o agregar la estrategia de particionamiento temporal requiere migración"
        )
    elif (
        current_time
        and desired_time
        and current_time.get("expirationMs") != desired_time.get("expirationMs")
    ):
        update_fields.append("time_partitioning")

    current_range = current.get("rangePartitioning")
    desired_range = resource.get("rangePartitioning")
    if current_range != desired_range:
        blockers.append("cambiar o agregar rangePartitioning requiere migración")

    if bool(current.get("requirePartitionFilter", False)) != bool(
        resource.get("requirePartitionFilter", False)
    ):
        update_fields.append("require_partition_filter")

    current_clustering = current.get("clustering", {}).get("fields", [])
    desired_clustering = resource.get("clustering", {}).get("fields", [])
    if current_clustering != desired_clustering:
        update_fields.append("clustering_fields")

    update_fields = list(dict.fromkeys(update_fields))
    action = (
        "blocked"
        if blockers
        else ("update" if update_fields or ddl_statements else "noop")
    )
    return TablePlan(
        artifact=artifact,
        desired_resource=resource,
        action=action,
        blockers=blockers,
        ddl_statements=ddl_statements,
        update_fields=update_fields,
    )


def import_gcp() -> tuple[Any, type[Exception]]:
    try:
        from google.api_core.exceptions import NotFound
        from google.cloud import bigquery
    except ImportError as exc:
        raise PreflightFailure(
            "Faltan dependencias de Google Cloud. Ejecute: pip install -r requirements.txt"
        ) from exc
    return bigquery, NotFound


def validate_datasets(
    client: Any, bigquery: Any, project: str, datasets: set[str], location: str
) -> list[dict[str, str]]:
    errors: list[str] = []
    results: list[dict[str, str]] = []
    for dataset_id in sorted(datasets):
        try:
            dataset = client.get_dataset(bigquery.DatasetReference(project, dataset_id))
        except Exception as exc:  # noqa: BLE001 - preserve all dataset failures in one report.
            errors.append(
                f"{project}.{dataset_id}: no fue posible obtener el dataset: {exc}"
            )
            continue
        actual_location = dataset.location or ""
        results.append(
            {"dataset": f"{project}.{dataset_id}", "location": actual_location}
        )
        if normalize_location(actual_location) != normalize_location(location):
            errors.append(
                f"{project}.{dataset_id}: ubicación real '{actual_location}' distinta de '{location}'"
            )
    if errors:
        raise PreflightFailure("Validación de datasets fallida:\n" + "\n".join(errors))
    return results


def plan_procedure(
    client: Any, not_found: type[Exception], project: str, artifact: ProcedureArtifact
) -> ProcedurePlan:
    try:
        client.get_routine(f"{project}.{artifact.dataset}.{artifact.name}")
        action = "replace"
    except not_found:
        action = "create"
    return ProcedurePlan(artifact=artifact, action=action)


def apply_table_plan(
    client: Any, bigquery: Any, plan: TablePlan, location: str
) -> None:
    resource = plan.desired_resource
    table_fqn = (
        f"{resource['tableReference']['projectId']}."
        f"{resource['tableReference']['datasetId']}."
        f"{resource['tableReference']['tableId']}"
    )
    if plan.action == "noop":
        return
    if plan.action == "create":
        client.create_table(bigquery.Table.from_api_repr(resource))
        return

    for statement in plan.ddl_statements:
        client.query(statement, location=location).result()

    current = client.get_table(table_fqn)
    desired = bigquery.Table.from_api_repr(resource)
    for update_field in plan.update_fields:
        setattr(current, update_field, getattr(desired, update_field))
    if plan.update_fields:
        client.update_table(current, plan.update_fields)


def apply_procedure(client: Any, artifact: ProcedureArtifact, location: str) -> None:
    client.query(artifact.sql, location=location).result()


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def append_github_summary(report: dict[str, Any]) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## BigQuery deployment",
        "",
        f"- Estado: `{report.get('status', 'unknown')}`",
        f"- Modo: `{report.get('mode', 'unknown')}`",
    ]
    if report.get("project"):
        lines.append(f"- Proyecto: `{report['project']}`")
    if report.get("location"):
        lines.append(f"- Ubicación: `{report['location']}`")
    if report.get("environment"):
        lines.append(f"- Ambiente: `{report['environment']}`")
    lines.extend(["", "### Tablas", ""])
    for item in report.get("tables", []):
        lines.append(f"- `{item['resource']}`: **{item['action']}**")
    lines.extend(["", "### Procedimientos", ""])
    for item in report.get("procedures", []):
        lines.append(f"- `{item['resource']}`: **{item['action']}**")
    if report.get("errors"):
        lines.extend(["", "### Errores", ""])
        lines.extend(f"- {error}" for error in report["errors"])
    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def print_plan(
    table_plans: list[TablePlan], procedure_plans: list[ProcedurePlan]
) -> None:
    print("Plan de tablas:")
    for plan in table_plans:
        print(f"  - {plan.artifact.key}: {plan.action}")
        for blocker in plan.blockers:
            print(f"      BLOQUEO: {blocker}")
        for statement in plan.ddl_statements:
            print(f"      DDL: {statement}")
        if plan.update_fields:
            print(f"      PATCH: {', '.join(plan.update_fields)}")
    print("Plan de procedimientos:")
    for plan in procedure_plans:
        print(f"  - {plan.artifact.key}: {plan.action}")


def execute(args: argparse.Namespace, report: dict[str, Any]) -> None:
    root = args.root.resolve()
    schema_path = args.schema.resolve() if args.schema else None
    artifacts = validate_repository(
        root,
        schema_path=schema_path,
        strict_artifact_repository=args.strict_artifact_repository,
    )
    report["tables"] = [
        {"resource": item.key, "file": item.path.as_posix(), "action": "validated"}
        for item in artifacts.tables
    ]
    report["procedures"] = [
        {"resource": item.key, "file": item.path.as_posix(), "action": "validated"}
        for item in artifacts.procedures
    ]

    if args.command == "validate":
        report["status"] = "validated"
        print(
            f"Validación exitosa: {len(artifacts.tables)} tabla(s), {len(artifacts.procedures)} procedimiento(s)"
        )
        return

    if not args.project or not args.location:
        raise PreflightFailure(
            "--project y --location son obligatorios para plan/apply"
        )

    bigquery, not_found = import_gcp()
    policy_mapping = load_policy_tag_mapping(args.policy_tags_json)
    try:
        client = bigquery.Client(project=args.project)
    except Exception as exc:
        raise PreflightFailure(
            f"No fue posible inicializar el cliente de BigQuery: {exc}"
        ) from exc
    datasets = {item.dataset for item in artifacts.tables} | {
        item.dataset for item in artifacts.procedures
    }
    report["datasets"] = validate_datasets(
        client, bigquery, args.project, datasets, args.location
    )

    resources: list[dict[str, Any]] = []
    policy_errors: list[str] = []
    for artifact in artifacts.tables:
        try:
            resources.append(
                manifest_to_table_resource(
                    artifact, args.project, args.location, policy_mapping
                )
            )
        except PreflightFailure as exc:
            policy_errors.append(str(exc))
    if policy_errors:
        raise PreflightFailure(
            "Resolución de policy tags fallida:\n" + "\n".join(policy_errors)
        )

    table_plans: list[TablePlan] = []
    for artifact, resource in zip(artifacts.tables, resources, strict=True):
        try:
            table_plans.append(
                plan_table(client, bigquery, not_found, artifact, resource)
            )
        except Exception as exc:
            raise PreflightFailure(
                f"No fue posible inspeccionar la tabla {artifact.key}: {exc}"
            ) from exc

    procedure_plans: list[ProcedurePlan] = []
    for artifact in artifacts.procedures:
        try:
            procedure_plans.append(
                plan_procedure(client, not_found, args.project, artifact)
            )
        except Exception as exc:
            raise PreflightFailure(
                f"No fue posible inspeccionar el procedimiento {artifact.key}: {exc}"
            ) from exc
    report["tables"] = [item.report() for item in table_plans]
    report["procedures"] = [item.report() for item in procedure_plans]
    print_plan(table_plans, procedure_plans)

    blockers = [
        f"{plan.artifact.key}: {blocker}"
        for plan in table_plans
        for blocker in plan.blockers
    ]
    if blockers:
        report["status"] = "blocked"
        raise BlockedChanges("Cambios bloqueados:\n" + "\n".join(blockers))

    if args.command == "plan":
        report["status"] = "planned"
        return

    report["applied"] = []
    for plan in table_plans:
        try:
            apply_table_plan(client, bigquery, plan, args.location)
        except Exception as exc:
            raise ApplyFailure(f"Falló la tabla {plan.artifact.key}: {exc}") from exc
        report["applied"].append(
            {"kind": "table", "resource": plan.artifact.key, "action": plan.action}
        )

    for plan in procedure_plans:
        try:
            apply_procedure(client, plan.artifact, args.location)
        except Exception as exc:
            raise ApplyFailure(
                f"Falló el procedimiento {plan.artifact.key}: {exc}"
            ) from exc
        report["applied"].append(
            {"kind": "procedure", "resource": plan.artifact.key, "action": plan.action}
        )

    try:
        for artifact in artifacts.tables:
            client.get_table(f"{args.project}.{artifact.dataset}.{artifact.name}")
        for artifact in artifacts.procedures:
            client.get_routine(f"{args.project}.{artifact.dataset}.{artifact.name}")
    except Exception as exc:
        raise ApplyFailure(
            f"La verificación posterior al despliegue falló: {exc}"
        ) from exc
    report["status"] = "applied"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "plan", "apply"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--schema",
        type=Path,
        help="Ruta al JSON Schema; por defecto usa <root>/schemas/table.schema.json",
    )
    parser.add_argument(
        "--strict-artifact-repository",
        action="store_true",
        help="Rechaza todo archivo distinto de tbl/<dataset>/*.json y sp/<dataset>/*.sql",
    )
    parser.add_argument("--project")
    parser.add_argument("--location")
    parser.add_argument("--environment", default="local")
    parser.add_argument(
        "--policy-tags-json",
        default=os.getenv("BQ_POLICY_TAGS_JSON", "{}"),
        help="Mapeo JSON taxonomy.policy_tag -> resource name",
    )
    parser.add_argument("--report", type=Path, default=Path("deployment-report.json"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report: dict[str, Any] = {
        "startedAt": utc_now(),
        "finishedAt": None,
        "mode": args.command,
        "environment": args.environment,
        "project": args.project,
        "location": args.location,
        "status": "running",
        "tables": [],
        "procedures": [],
        "errors": [],
    }
    exit_code = 0
    try:
        execute(args, report)
    except DeploymentFailure as exc:
        exit_code = exc.exit_code
        report["errors"] = (
            exc.errors if isinstance(exc, ValidationFailure) else [str(exc)]
        )
        if report["status"] == "running":
            report["status"] = "failed"
        print(str(exc), file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - last-resort report for unknown failures.
        exit_code = EXIT_UNEXPECTED
        report["status"] = "failed"
        report["errors"] = [f"Error inesperado: {exc}"]
        traceback.print_exc()
    finally:
        report["finishedAt"] = utc_now()
        write_report(args.report, report)
        append_github_summary(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
