import copy
import tempfile
import unittest
from pathlib import Path

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from scripts.bq_deploy import (
    PreflightFailure,
    ValidationFailure,
    compare_schema_fields,
    manifest_to_table_resource,
    parameter_change_is_widening,
    plan_table,
    validate_repository,
)


class RepositoryValidationTests(unittest.TestCase):
    def test_repository_examples_are_valid(self):
        root = Path(__file__).resolve().parents[1]
        artifacts = validate_repository(root)
        self.assertEqual([item.key for item in artifacts.tables], ["core.clientes"])
        self.assertEqual(
            [item.key for item in artifacts.procedures], ["core.sp_upsert_cliente"]
        )

    def test_dataset_path_mismatch_is_rejected(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "schemas").mkdir()
            (target / "tbl" / "otro_dataset").mkdir(parents=True)
            (target / "sp").mkdir()
            (target / "schemas" / "table.schema.json").write_text(
                (root / "schemas" / "table.schema.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            manifest = (root / "tbl" / "core" / "clientes.json").read_text(
                encoding="utf-8"
            )
            (target / "tbl" / "otro_dataset" / "clientes.json").write_text(
                manifest, encoding="utf-8"
            )
            with self.assertRaises(ValidationFailure) as context:
                validate_repository(target)
            self.assertIn("metadata.dataset", str(context.exception))

    def test_policy_tag_alias_must_exist_for_the_selected_environment(self):
        root = Path(__file__).resolve().parents[1]
        artifact = validate_repository(root).tables[0]
        with self.assertRaises(PreflightFailure) as context:
            manifest_to_table_resource(
                artifact,
                "example-dev-project",
                "southamerica-west1",
                {},
            )
        self.assertIn("customer_data.email", str(context.exception))

    def test_policy_tag_location_must_match_dataset_location(self):
        root = Path(__file__).resolve().parents[1]
        artifact = validate_repository(root).tables[0]
        with self.assertRaises(PreflightFailure) as context:
            manifest_to_table_resource(
                artifact,
                "example-dev-project",
                "southamerica-west1",
                {
                    "customer_data.email": (
                        "projects/123/locations/us/taxonomies/456/policyTags/789"
                    )
                },
            )
        self.assertIn("no en 'southamerica-west1'", str(context.exception))

    def test_strict_artifact_repository_rejects_extra_files(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "tbl" / "core").mkdir(parents=True)
            (target / "sp" / "core").mkdir(parents=True)
            (target / "tbl" / "core" / "clientes.json").write_text(
                (root / "tbl" / "core" / "clientes.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (target / "sp" / "core" / "010_sp_upsert_cliente.sql").write_text(
                (root / "sp" / "core" / "010_sp_upsert_cliente.sql").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            (target / "README.md").write_text("not allowed\n", encoding="utf-8")

            with self.assertRaises(ValidationFailure) as context:
                validate_repository(
                    target,
                    schema_path=root / "schemas" / "table.schema.json",
                    strict_artifact_repository=True,
                )
            self.assertIn("README.md", str(context.exception))


class SchemaReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.current = [
            {
                "name": "id",
                "type": "STRING",
                "mode": "REQUIRED",
                "maxLength": "36",
            }
        ]

    def test_nullable_column_can_be_appended(self):
        desired = copy.deepcopy(self.current)
        desired.append({"name": "payload", "type": "JSON", "mode": "NULLABLE"})
        blockers, _, changed = compare_schema_fields(
            self.current, desired, "project.dataset.table"
        )
        self.assertEqual(blockers, [])
        self.assertTrue(changed)

    def test_required_column_cannot_be_appended(self):
        desired = copy.deepcopy(self.current)
        desired.append({"name": "created_at", "type": "TIMESTAMP", "mode": "REQUIRED"})
        blockers, _, _ = compare_schema_fields(
            self.current, desired, "project.dataset.table"
        )
        self.assertTrue(any("NULLABLE o REPEATED" in item for item in blockers))

    def test_column_deletion_is_blocked(self):
        blockers, _, _ = compare_schema_fields(
            self.current,
            [],
            "project.dataset.table",
        )
        self.assertTrue(any("eliminar columnas" in item for item in blockers))

    def test_string_length_can_only_be_widened(self):
        current = {"name": "name", "type": "STRING", "maxLength": "10"}
        wider = {"name": "name", "type": "STRING", "maxLength": "20"}
        narrower = {"name": "name", "type": "STRING", "maxLength": "5"}
        self.assertEqual(parameter_change_is_widening(current, wider), (True, True))
        self.assertEqual(parameter_change_is_widening(current, narrower), (True, False))

    def test_numeric_precision_must_be_less_restrictive(self):
        current = {
            "name": "amount",
            "type": "NUMERIC",
            "precision": "10",
            "scale": "2",
        }
        wider = {
            "name": "amount",
            "type": "NUMERIC",
            "precision": "12",
            "scale": "4",
        }
        less_integer_capacity = {
            "name": "amount",
            "type": "NUMERIC",
            "precision": "10",
            "scale": "4",
        }
        self.assertEqual(parameter_change_is_widening(current, wider), (True, True))
        self.assertEqual(
            parameter_change_is_widening(current, less_integer_capacity), (True, False)
        )

    def test_official_client_accepts_every_supported_manifest_type(self):
        fields = [
            {"name": "string_value", "type": "STRING", "maxLength": "100"},
            {"name": "bytes_value", "type": "BYTES", "maxLength": "32"},
            {"name": "integer_value", "type": "INT64"},
            {"name": "float_value", "type": "FLOAT64"},
            {"name": "boolean_value", "type": "BOOL"},
            {
                "name": "numeric_value",
                "type": "NUMERIC",
                "precision": "15",
                "scale": "6",
            },
            {
                "name": "bignumeric_value",
                "type": "BIGNUMERIC",
                "precision": "40",
                "scale": "10",
            },
            {"name": "date_value", "type": "DATE"},
            {"name": "time_value", "type": "TIME"},
            {"name": "datetime_value", "type": "DATETIME"},
            {"name": "timestamp_value", "type": "TIMESTAMP"},
            {"name": "geography_value", "type": "GEOGRAPHY"},
            {"name": "json_value", "type": "JSON"},
            {"name": "interval_value", "type": "INTERVAL"},
            {
                "name": "range_value",
                "type": "RANGE",
                "rangeElementType": {"type": "TIMESTAMP"},
            },
            {
                "name": "struct_value",
                "type": "RECORD",
                "fields": [{"name": "nested", "type": "STRING"}],
            },
            {"name": "array_value", "type": "STRING", "mode": "REPEATED"},
        ]
        parsed = [bigquery.SchemaField.from_api_repr(item) for item in fields]
        self.assertEqual(len(parsed), len(fields))
        self.assertEqual(parsed[12].field_type, "JSON")
        self.assertEqual(parsed[13].field_type, "INTERVAL")
        self.assertEqual(parsed[15].field_type, "RECORD")
        self.assertEqual(parsed[16].mode, "REPEATED")

    def test_identical_bigquery_resource_produces_noop_plan(self):
        root = Path(__file__).resolve().parents[1]
        artifact = validate_repository(root).tables[0]
        resource = manifest_to_table_resource(
            artifact,
            "example-dev-project",
            "southamerica-west1",
            {
                "customer_data.email": (
                    "projects/123/locations/southamerica-west1/"
                    "taxonomies/456/policyTags/789"
                )
            },
        )

        class FakeClient:
            def get_table(self, _):
                return bigquery.Table.from_api_repr(resource)

        plan = plan_table(FakeClient(), bigquery, NotFound, artifact, resource)
        self.assertEqual(plan.action, "noop")
        self.assertEqual(plan.blockers, [])


if __name__ == "__main__":
    unittest.main()
