"""All built-in SDLC agent role classes.

Each class is a minimal subclass of BaseSDLCAgent that declares:
  - ROLE / ROLE_DISPLAY — identity
  - SKILL_MD_PATH — path to SKILL.md (loaded into system prompt)
  - AUTHORITY_STATEMENT — what this role is empowered to decide
  - OUTPUT_SCHEMA — JSON Schema for the artifact this role must produce
  - ENABLED_TOOLSETS — which tool groups this role may access
  - prior_stages() — which stage artifacts feed into this role

The run() loop is inherited from BaseSDLCAgent.
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.agent_base import BaseSDLCAgent, AgentConfig
from core.memory import ArtifactBundle, ProjectMemory
from core.session import PipelineDB


# ---------------------------------------------------------------------------
# Shared toolset constants
# ---------------------------------------------------------------------------

_READ_ONLY = ["read_file", "search_files", "web_search", "memory_read"]
_WRITE = _READ_ONLY + ["write_file", "patch"]
_FULL = _WRITE + ["terminal", "execute_code"]
_KANBAN = ["kanban_create", "kanban_complete", "kanban_block", "kanban_comment"]


# ---------------------------------------------------------------------------
# Discovery stage
# ---------------------------------------------------------------------------

class DomainExpertAgent(BaseSDLCAgent):
    ROLE = "domain_expert"
    ROLE_DISPLAY = "Domain Expert"
    SKILL_MD_PATH = "skills/domain-expert/SKILL.md"
    AUTHORITY_STATEMENT = (
        "You are empowered to define and document the business domain: "
        "entities, bounded contexts, terminology, and constraints. "
        "You do NOT make technology decisions."
    )
    ENABLED_TOOLSETS = _READ_ONLY
    MAX_ITERATIONS = 20
    OUTPUT_SCHEMA = {
        "type": "object",
        "required": ["glossary", "bounded_contexts"],
        "properties": {
            "glossary": {
                "type": "object",
                "description": "Term → definition mapping",
                "additionalProperties": {"type": "string"},
            },
            "bounded_contexts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "entities", "responsibilities"],
                    "properties": {
                        "name": {"type": "string"},
                        "entities": {"type": "array", "items": {"type": "string"}},
                        "responsibilities": {"type": "string"},
                    },
                },
            },
            "key_constraints": {"type": "array", "items": {"type": "string"}},
            "out_of_scope": {"type": "array", "items": {"type": "string"}},
        },
    }

    def prior_stages(self) -> List[str]:
        return []


class ProductOwnerAgent(BaseSDLCAgent):
    ROLE = "product_owner"
    ROLE_DISPLAY = "Product Owner"
    SKILL_MD_PATH = "skills/product-owner/SKILL.md"
    AUTHORITY_STATEMENT = (
        "You own the product backlog. You prioritise user stories and define "
        "MVP scope. Every story must have Gherkin acceptance criteria."
    )
    ENABLED_TOOLSETS = _WRITE
    MAX_ITERATIONS = 20
    OUTPUT_SCHEMA = {
        "type": "object",
        "required": ["user_stories", "mvp_stories"],
        "properties": {
            "user_stories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "title", "story", "priority", "acceptance_criteria"],
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "story": {"type": "string"},
                        "priority": {"type": "string", "enum": ["must", "should", "could", "wont"]},
                        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "mvp_stories": {"type": "array", "items": {"type": "string"}},
            "mvp_rationale": {"type": "string"},
        },
    }

    def prior_stages(self) -> List[str]:
        return ["discovery"]


# ---------------------------------------------------------------------------
# Requirements stage
# ---------------------------------------------------------------------------

class BusinessAnalystAgent(BaseSDLCAgent):
    ROLE = "business_analyst"
    ROLE_DISPLAY = "Business Analyst"
    SKILL_MD_PATH = "skills/business-analyst/SKILL.md"
    AUTHORITY_STATEMENT = (
        "You produce the Business Requirements Specification. Every requirement "
        "must be uniquely numbered, testable, and traceable to a user story. "
        "Flag ambiguities explicitly."
    )
    ENABLED_TOOLSETS = _WRITE
    MAX_ITERATIONS = 25
    OUTPUT_SCHEMA = {
        "type": "object",
        "required": ["functional_requirements", "non_functional_requirements", "data_dictionary"],
        "properties": {
            "functional_requirements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "story_ref", "description", "priority"],
                    "properties": {
                        "id": {"type": "string"},
                        "story_ref": {"type": "string"},
                        "description": {"type": "string"},
                        "priority": {"type": "string"},
                    },
                },
            },
            "non_functional_requirements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "category", "description"],
                    "properties": {
                        "id": {"type": "string"},
                        "category": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            },
            "data_dictionary": {"type": "array", "items": {"type": "object"}},
            "process_flows": {"type": "array", "items": {"type": "object"}},
            "open_questions": {"type": "array", "items": {"type": "string"}},
            "assumptions": {"type": "array", "items": {"type": "string"}},
        },
    }

    def prior_stages(self) -> List[str]:
        return ["discovery"]


# ---------------------------------------------------------------------------
# Architecture stage
# ---------------------------------------------------------------------------

class ArchitectAgent(BaseSDLCAgent):
    ROLE = "architect"
    ROLE_DISPLAY = "Architect"
    SKILL_MD_PATH = "skills/architect/SKILL.md"
    AUTHORITY_STATEMENT = (
        "You produce Architecture Decision Records and C4 diagrams. "
        "Every technology choice requires an ADR. You do NOT write code."
    )
    ENABLED_TOOLSETS = _WRITE + ["web_search"]
    MAX_ITERATIONS = 50
    OUTPUT_SCHEMA = {
        "type": "object",
        "required": ["adrs", "tech_stack"],
        "properties": {
            "adrs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "title", "status", "context", "decision", "consequences"],
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "status": {"type": "string"},
                        "context": {"type": "string"},
                        "options_considered": {"type": "array", "items": {"type": "string"}},
                        "decision": {"type": "string"},
                        "consequences": {"type": "object"},
                    },
                },
            },
            "c4_context": {"type": "string"},
            "c4_container": {"type": "string"},
            "tech_stack": {"type": "object"},
            "api_style": {"type": "string"},
            "auth_strategy": {"type": "string"},
        },
    }

    def prior_stages(self) -> List[str]:
        return ["discovery", "requirements"]


# ---------------------------------------------------------------------------
# Planning stage
# ---------------------------------------------------------------------------

class TechLeadAgent(BaseSDLCAgent):
    ROLE = "tech_lead"
    ROLE_DISPLAY = "Tech Lead"
    SKILL_MD_PATH = "skills/tech-lead/SKILL.md"
    AUTHORITY_STATEMENT = (
        "You decompose the architecture into shippable task cards. "
        "Each task must be self-contained, assignable to one developer role, "
        "and completable in <4 hours of AI agent work."
    )
    ENABLED_TOOLSETS = _WRITE + _KANBAN
    MAX_ITERATIONS = 30
    OUTPUT_SCHEMA = {
        "type": "object",
        "required": ["tasks", "coding_standards"],
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["task_id", "title", "assignee_role", "description", "acceptance_criteria"],
                    "properties": {
                        "task_id": {"type": "string"},
                        "title": {"type": "string"},
                        "assignee_role": {
                            "type": "string",
                            "enum": ["developer_backend", "developer_ui", "developer_devops"],
                        },
                        "description": {"type": "string"},
                        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                        "dependencies": {"type": "array", "items": {"type": "string"}},
                        "skills": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "coding_standards": {"type": "object"},
            "sprint_plan": {"type": "array", "items": {"type": "object"}},
        },
    }

    def prior_stages(self) -> List[str]:
        return ["discovery", "requirements", "architecture"]


# ---------------------------------------------------------------------------
# Development stage — three parallel roles
# ---------------------------------------------------------------------------

class _DeveloperBase(BaseSDLCAgent):
    """Shared base for all developer roles."""
    ENABLED_TOOLSETS = _FULL
    MAX_ITERATIONS = 30
    OUTPUT_SCHEMA = {
        "type": "object",
        "required": ["files_written", "tests_passing"],
        "properties": {
            "files_written": {"type": "array", "items": {"type": "string"}},
            "tests_passing": {"type": "boolean"},
            "coverage_pct": {"type": "number"},
            "pr_description": {"type": "string"},
            "notes": {"type": "string"},
        },
    }

    def prior_stages(self) -> List[str]:
        return ["discovery", "requirements", "architecture", "planning"]


class DeveloperBackendAgent(_DeveloperBase):
    ROLE = "developer_backend"
    ROLE_DISPLAY = "Backend Developer"
    SKILL_MD_PATH = "skills/developer-backend/SKILL.md"
    AUTHORITY_STATEMENT = (
        "You implement API endpoints, business logic, database schemas, and migrations. "
        "Write production-quality code with ≥80% test coverage. "
        "Include OpenAPI doc comments on every endpoint."
    )


class DeveloperUIAgent(_DeveloperBase):
    ROLE = "developer_ui"
    ROLE_DISPLAY = "UI Developer"
    SKILL_MD_PATH = "skills/developer-ui/SKILL.md"
    AUTHORITY_STATEMENT = (
        "You implement frontend components. Every interactive element must be "
        "keyboard-accessible and include ARIA labels. Write unit tests for logic "
        "and Playwright tests for critical user flows."
    )
    ENABLED_TOOLSETS = _FULL + ["browser_navigate", "browser_snapshot", "browser_click",
                                 "browser_type"]


class DeveloperDevOpsAgent(_DeveloperBase):
    ROLE = "developer_devops"
    ROLE_DISPLAY = "DevOps Engineer"
    SKILL_MD_PATH = "skills/developer-devops/SKILL.md"
    AUTHORITY_STATEMENT = (
        "You produce Dockerfile, docker-compose, GitHub Actions CI pipeline, "
        "and infrastructure as code. Never hardcode secrets."
    )
    ENABLED_TOOLSETS = _FULL + ["devops", "github"]
    OUTPUT_SCHEMA = {
        "type": "object",
        "required": ["files_written"],
        "properties": {
            "files_written": {"type": "array", "items": {"type": "string"}},
            "ci_steps": {"type": "array", "items": {"type": "string"}},
            "secrets_required": {"type": "array", "items": {"type": "string"}},
            "deployment_notes": {"type": "string"},
        },
    }


# ---------------------------------------------------------------------------
# QA stage
# ---------------------------------------------------------------------------

class QAEngineerAgent(BaseSDLCAgent):
    ROLE = "qa_engineer"
    ROLE_DISPLAY = "QA Engineer"
    SKILL_MD_PATH = "skills/qa-engineer/SKILL.md"
    AUTHORITY_STATEMENT = (
        "You review all produced code and artifacts, generate a test plan from "
        "acceptance criteria, run tests, and report coverage. "
        "The quality gate passes only when all must-have AC are covered and "
        "coverage ≥ configured threshold."
    )
    ENABLED_TOOLSETS = _WRITE + ["terminal", "execute_code", "qa"]
    MAX_ITERATIONS = 40
    OUTPUT_SCHEMA = {
        "type": "object",
        "required": ["coverage_pct", "gate_passed", "bugs"],
        "properties": {
            "test_plan": {"type": "string"},
            "test_suites_run": {"type": "array", "items": {"type": "string"}},
            "total_tests": {"type": "integer"},
            "passing": {"type": "integer"},
            "failing": {"type": "integer"},
            "coverage_pct": {"type": "number"},
            "failing_tests": {
                "type": "array",
                "description": "Machine-readable list of failing test cases for the fix loop.",
                "items": {
                    "type": "object",
                    "properties": {
                        "nodeid": {"type": "string"},
                        "file": {"type": "string"},
                        "message": {"type": "string"},
                    },
                },
            },
            "bugs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "title", "severity"],
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "severity": {"type": "string", "enum": ["critical", "major", "minor", "trivial"]},
                        "steps_to_reproduce": {"type": "string"},
                        "related_task": {"type": "string"},
                    },
                },
            },
            "acceptance_criteria_coverage": {"type": "object"},
            "gate_passed": {"type": "boolean"},
            "gate_rationale": {"type": "string"},
        },
    }

    def prior_stages(self) -> List[str]:
        return ["discovery", "requirements", "architecture", "planning", "development"]


# ---------------------------------------------------------------------------
# Delivery stage
# ---------------------------------------------------------------------------

class ReleaseManagerAgent(BaseSDLCAgent):
    ROLE = "release_manager"
    ROLE_DISPLAY = "Release Manager"
    SKILL_MD_PATH = "skills/release-manager/SKILL.md"
    AUTHORITY_STATEMENT = (
        "You produce release notes, a deployment checklist, and a rollback plan. "
        "You do NOT write application code."
    )
    ENABLED_TOOLSETS = _WRITE + ["terminal"]
    MAX_ITERATIONS = 20
    OUTPUT_SCHEMA = {
        "type": "object",
        "required": ["version", "release_notes", "deployment_checklist"],
        "properties": {
            "version": {"type": "string"},
            "release_notes": {"type": "string"},
            "deployment_checklist": {"type": "array", "items": {"type": "string"}},
            "rollback_procedure": {"type": "string"},
            "release_tag": {"type": "string"},
            "deployment_manifest": {"type": "string"},
        },
    }

    def prior_stages(self) -> List[str]:
        return ["discovery", "requirements", "architecture", "planning",
                "development", "quality_gate"]


# ---------------------------------------------------------------------------
# Role map — used by pipeline._register_builtin_agents()
# ---------------------------------------------------------------------------

AGENT_ROLE_MAP: Dict[str, type] = {
    "domain_expert":      DomainExpertAgent,
    "product_owner":      ProductOwnerAgent,
    "business_analyst":   BusinessAnalystAgent,
    "architect":          ArchitectAgent,
    "tech_lead":          TechLeadAgent,
    "developer_backend":  DeveloperBackendAgent,
    "developer_ui":       DeveloperUIAgent,
    "developer_devops":   DeveloperDevOpsAgent,
    "qa_engineer":        QAEngineerAgent,
    "release_manager":    ReleaseManagerAgent,
}
