"""File-request allocation, capability contracts, and tool-spec dialects."""

from __future__ import annotations

import unittest

import loomcraft as lc
from loomcraft.errors import ContractError, InputRequestError, RegistryError
from loomcraft.inputs import allocate_uploads, validate_fulfillment, validate_input_request


def requirement(key, *, extensions=(), required=True, minimum=1, maximum=1, label=None):
    return {
        "key": key,
        "label": label or key.replace("_", " ").title(),
        "description": f"The {key} file.",
        "required": required,
        "min_files": minimum if required else 0,
        "max_files": maximum,
        "allowed_extensions": list(extensions),
        "field_hints": [],
    }


def request(*requirements, title="Need files"):
    return {
        "title": title,
        "message": "Please upload the files listed below.",
        "requirements": list(requirements),
        "continue_prompt": "Files are ready, continue.",
    }


def upload(upload_id, filename):
    return {"id": upload_id, "filename": filename, "checksum": f"sha-{upload_id}"}


class TestInputRequestValidation(unittest.TestCase):
    def test_valid_request_gets_a_server_generated_id(self):
        validated = validate_input_request(request(requirement("table")))
        self.assertRegex(validated["request_id"], r"^input-[0-9a-f]{16}$")

    def test_model_supplied_id_is_rejected(self):
        payload = request(requirement("table"))
        payload["request_id"] = "input-0000000000000000"
        with self.assertRaises(InputRequestError):
            validate_input_request(payload)

    def test_duplicate_keys_are_rejected(self):
        with self.assertRaises(InputRequestError):
            validate_input_request(request(requirement("a"), requirement("a")))

    def test_bad_extension_is_rejected(self):
        with self.assertRaises(InputRequestError):
            validate_input_request(request(requirement("a", extensions=("csv",))))

    def test_required_slot_must_have_a_minimum(self):
        payload = request(requirement("a"))
        payload["requirements"][0]["min_files"] = 0
        with self.assertRaises(InputRequestError):
            validate_input_request(payload)

    def test_max_below_min_is_rejected(self):
        payload = request(requirement("a", minimum=3, maximum=2))
        with self.assertRaises(InputRequestError):
            validate_input_request(payload)


class TestAllocation(unittest.TestCase):
    def test_single_slot_single_file(self):
        payload = validate_input_request(request(requirement("table", extensions=(".csv",))))
        allocated = allocate_uploads(payload, [upload("u1", "data.csv")])
        self.assertEqual(allocated["table"], ["u1"])

    def test_extension_filtering_excludes_non_matching_files(self):
        payload = validate_input_request(request(requirement("table", extensions=(".csv",))))
        allocated = allocate_uploads(payload, [upload("u1", "notes.txt")])
        self.assertEqual(allocated["table"], [])

    def test_augmenting_path_releases_a_contended_file(self):
        # A permissive slot listed first must give the .csv back to the strict
        # slot; naive first-match assignment leaves `strict` empty.
        payload = validate_input_request(
            request(
                requirement("permissive", extensions=(".csv", ".tsv")),
                requirement("strict", extensions=(".csv",)),
            )
        )
        allocated = allocate_uploads(
            payload, [upload("u1", "alpha.csv"), upload("u2", "beta.tsv")]
        )
        self.assertEqual(allocated["strict"], ["u1"])
        self.assertEqual(allocated["permissive"], ["u2"])

    def test_filename_similarity_breaks_ties(self):
        payload = validate_input_request(
            request(
                requirement("pedigree", extensions=(".csv",)),
                requirement("phenotype", extensions=(".csv",)),
            )
        )
        allocated = allocate_uploads(
            payload,
            [upload("u1", "phenotype_2024.csv"), upload("u2", "pedigree_2024.csv")],
        )
        self.assertEqual(allocated["pedigree"], ["u2"])
        self.assertEqual(allocated["phenotype"], ["u1"])

    def test_duplicate_content_is_counted_once(self):
        payload = validate_input_request(
            request(
                requirement("a", extensions=(".csv",)),
                requirement("b", extensions=(".csv",)),
            )
        )
        same = {"id": "u1", "filename": "x.csv", "checksum": "same"}
        other = {"id": "u2", "filename": "y.csv", "checksum": "same"}
        allocated = allocate_uploads(payload, [same, other])
        assigned = allocated["a"] + allocated["b"]
        self.assertEqual(len(assigned), 1, "identical content may fill only one slot")

    def test_optional_slot_gets_a_leftover_file(self):
        payload = validate_input_request(
            request(
                requirement("main", extensions=(".csv",)),
                requirement("extra", extensions=(".csv",), required=False),
            )
        )
        allocated = allocate_uploads(
            payload, [upload("u1", "main.csv"), upload("u2", "other.csv")]
        )
        self.assertEqual(allocated["main"], ["u1"])
        self.assertEqual(allocated["extra"], ["u2"])

    def test_flexible_slot_absorbs_multiple_files(self):
        payload = validate_input_request(
            request(requirement("tables", extensions=(".csv",), minimum=1, maximum=3))
        )
        allocated = allocate_uploads(
            payload,
            [upload("u1", "a.csv"), upload("u2", "b.csv"), upload("u3", "c.csv")],
        )
        self.assertEqual(len(allocated["tables"]), 3)

    def test_max_files_is_respected(self):
        payload = validate_input_request(
            request(requirement("tables", extensions=(".csv",), minimum=1, maximum=2))
        )
        allocated = allocate_uploads(
            payload,
            [upload("u1", "a.csv"), upload("u2", "b.csv"), upload("u3", "c.csv")],
        )
        self.assertEqual(len(allocated["tables"]), 2)

    def test_no_extension_constraint_accepts_anything(self):
        payload = validate_input_request(request(requirement("any")))
        allocated = allocate_uploads(payload, [upload("u1", "mystery.bin")])
        self.assertEqual(allocated["any"], ["u1"])


class TestFulfillment(unittest.TestCase):
    def test_satisfied_request_passes(self):
        payload = validate_input_request(request(requirement("table", extensions=(".csv",))))
        allocated = validate_fulfillment(payload, [upload("u1", "data.csv")])
        self.assertEqual(allocated["table"], ["u1"])

    def test_missing_required_slot_raises_with_the_label(self):
        payload = validate_input_request(
            request(requirement("table", extensions=(".csv",), label="Source table"))
        )
        with self.assertRaises(lc.InputRequestError) as ctx:
            validate_fulfillment(payload, [upload("u1", "notes.txt")])
        self.assertIn("Source table", str(ctx.exception))

    def test_optional_slot_does_not_block(self):
        payload = validate_input_request(
            request(
                requirement("table", extensions=(".csv",)),
                requirement("notes", extensions=(".md",), required=False),
            )
        )
        validate_fulfillment(payload, [upload("u1", "data.csv")])


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = lc.Registry()

        async def noop(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok()

        self.registry.register_runner("noop", noop)

    def capability(self, **overrides):
        base = dict(
            id="demo.thing",
            name="Demo",
            description="Does a demo thing.",
            runner="noop",
        )
        base.update(overrides)
        return lc.Capability(**base)

    def test_duplicate_registration_is_rejected(self):
        self.registry.register_capability(self.capability())
        with self.assertRaises(RegistryError):
            self.registry.register_capability(self.capability())

    def test_replace_allows_overriding(self):
        self.registry.register_capability(self.capability())
        self.registry.register_capability(
            self.capability(name="Demo v2"), replace=True
        )
        self.assertEqual(self.registry.capability("demo.thing").name, "Demo v2")

    def test_validate_reports_dangling_runner(self):
        self.registry.register_capability(self.capability(runner="missing.runner"))
        problems = self.registry.validate()
        self.assertTrue(any("unknown runner" in problem for problem in problems))

    def test_decorator_registers_both_halves(self):
        registry = lc.Registry()

        @registry.capability_runner(
            lc.Capability(
                id="deco.thing", name="Deco", description="Decorated.", runner="deco.run"
            )
        )
        async def handler(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok()

        self.assertTrue(registry.has_capability("deco.thing"))
        self.assertTrue(registry.has_runner("deco.run"))
        self.assertEqual(registry.validate(), [])

    def test_search_ranks_id_and_tag_matches_highest(self):
        self.registry.register_capability(
            self.capability(id="csv.profile", name="Profile CSV", tags=("csv", "profile"))
        )
        self.registry.register_capability(
            self.capability(id="pdf.extract", name="Extract PDF", tags=("pdf",))
        )
        results = self.registry.search("csv profile")
        self.assertEqual(results[0]["id"], "csv.profile")

    def test_merge_combines_catalogs(self):
        other = lc.Registry()

        async def noop(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok()

        other.register_runner("noop", noop)
        other.register_capability(self.capability(id="other.thing"))
        self.registry.register_capability(self.capability())
        merged = lc.merge_registries(self.registry, other)
        self.assertEqual(len(merged.capabilities), 2)
        self.assertEqual(merged.validate(), [])


class TestCapabilityContracts(unittest.TestCase):
    def setUp(self):
        self.capability = lc.Capability(
            id="geno.qc",
            name="Genotype QC",
            description="Quality control on genotype data.",
            runner="noop",
            inputs=(
                lc.CapabilityInput(key="bed", name="BED", description="PLINK bed", allowed_extensions=(".bed",)),
                lc.CapabilityInput(key="bim", name="BIM", description="PLINK bim", allowed_extensions=(".bim",)),
                lc.CapabilityInput(key="vcf", name="VCF", description="VCF", allowed_extensions=(".vcf",)),
            ),
            input_variants=(("bed", "bim"), ("vcf",)),
            parameters={
                "maf": lc.Parameter(type="number", description="MAF", minimum=0, maximum=0.5, default=0.01),
                "mode": lc.Parameter(type="string", description="Mode", enum=("strict", "loose")),
            },
        )

    def test_accepts_a_complete_variant(self):
        resolved = self.capability.validate_inputs({"bed": "upload:1", "bim": "upload:2"})
        self.assertEqual(resolved["bed"], ["upload:1"])

    def test_accepts_the_alternate_variant(self):
        resolved = self.capability.validate_inputs({"vcf": "upload:3"})
        self.assertEqual(resolved["vcf"], ["upload:3"])

    def test_rejects_a_partial_variant(self):
        with self.assertRaises(ContractError) as ctx:
            self.capability.validate_inputs({"bed": "upload:1"})
        self.assertIn("variant", str(ctx.exception))

    def test_rejects_mixing_variants(self):
        with self.assertRaises(ContractError):
            self.capability.validate_inputs(
                {"bed": "upload:1", "bim": "upload:2", "vcf": "upload:3"}
            )

    def test_rejects_unknown_input_key(self):
        with self.assertRaises(ContractError):
            self.capability.validate_inputs({"vcf": "upload:1", "bogus": "upload:2"})

    def test_rejects_duplicate_source_in_one_key(self):
        capability = lc.Capability(
            id="multi.thing",
            name="Multi",
            description="Takes several files.",
            runner="noop",
            inputs=(lc.CapabilityInput(key="docs", name="Docs", description="Docs", max_files=3),),
        )
        with self.assertRaises(ContractError):
            capability.validate_inputs({"docs": ["upload:1", "upload:1"]})

    def test_parameters_apply_defaults(self):
        parameters = self.capability.validate_parameters({})
        self.assertEqual(parameters["maf"], 0.01)

    def test_parameters_enforce_bounds(self):
        with self.assertRaises(ContractError):
            self.capability.validate_parameters({"maf": 0.9})

    def test_parameters_enforce_enums(self):
        with self.assertRaises(ContractError):
            self.capability.validate_parameters({"mode": "chaotic"})

    def test_parameters_enforce_types(self):
        with self.assertRaises(ContractError):
            self.capability.validate_parameters({"maf": "a lot"})

    def test_unknown_parameter_is_rejected(self):
        with self.assertRaises(ContractError):
            self.capability.validate_parameters({"unknown": 1})

    def test_contract_is_agent_readable(self):
        contract = self.capability.contract()
        self.assertEqual(contract["execution_tool"], "run_capability")
        self.assertEqual(len(contract["input_variants"]), 2)
        self.assertIn("maf", contract["parameters"])

    def test_workflow_rejects_unknown_input_reference(self):
        with self.assertRaises(Exception):
            lc.Workflow(
                id="bad.flow",
                name="Bad",
                description="References a missing input.",
                inputs=(lc.CapabilityInput(key="a", name="A", description="A"),),
                nodes=(lc.WorkflowNode(id="n", name="N", runner="noop", inputs=("ghost",)),),
            )

    def test_workflow_rejects_cycles(self):
        with self.assertRaises(Exception):
            lc.Workflow(
                id="cyc.flow",
                name="Cyclic",
                description="Cyclic workflow.",
                nodes=(
                    lc.WorkflowNode(id="a", name="A", runner="noop", depends_on=("b",)),
                    lc.WorkflowNode(id="b", name="B", runner="noop", depends_on=("a",)),
                ),
            )


class TestToolSpecs(unittest.TestCase):
    def test_canonical_surface(self):
        names = [spec.name for spec in lc.tool_specs()]
        self.assertIn("publish_plan", names)
        self.assertIn("run_capability", names)
        self.assertIn("register_artifacts", names)

    def test_workflows_can_be_excluded(self):
        names = [spec.name for spec in lc.tool_specs(include_workflows=False)]
        self.assertNotIn("run_workflow", names)

    def test_anthropic_dialect_shape(self):
        tool = lc.anthropic_tools()[0]
        self.assertIn("input_schema", tool)
        self.assertIn("name", tool)
        self.assertNotIn("parameters", tool)

    def test_openai_dialect_shape(self):
        tool = lc.openai_tools()[0]
        self.assertEqual(tool["type"], "function")
        self.assertIn("parameters", tool["function"])

    def test_mcp_dialect_shape(self):
        tool = lc.mcp_tools()[0]
        self.assertIn("inputSchema", tool)

    def test_every_schema_forbids_extra_properties(self):
        for spec in lc.tool_specs():
            self.assertFalse(
                spec.parameters.get("additionalProperties", True),
                f"{spec.name} should forbid extra properties",
            )

    def test_plan_schema_matches_the_model_bounds(self):
        steps = lc.tools.PLAN_SCHEMA["properties"]["steps"]
        self.assertEqual(steps["maxItems"], lc.plan.MAX_STEPS)
        self.assertEqual(
            lc.tools.PLAN_SCHEMA["properties"]["revision"]["maximum"],
            lc.plan.MAX_REVISION,
        )

    def test_unknown_dialect_raises(self):
        with self.assertRaises(ValueError):
            lc.to_dialect(lc.tool_specs(), "smoke-signals")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
