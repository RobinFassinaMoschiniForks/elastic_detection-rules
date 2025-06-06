# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License
# 2.0; you may not use this file except in compliance with the Elastic License
# 2.0.
"""Rule object."""
import copy
import dataclasses
import json
import os
import re
import time
import typing
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from urllib.parse import urlparse
from uuid import uuid4

import eql
import marshmallow
from semver import Version
from marko.block import Document as MarkoDocument
from marko.ext.gfm import gfm
from marshmallow import ValidationError, pre_load, validates_schema

import kql

from . import beats, ecs, endgame, utils
from .config import load_current_package_version, parse_rules_config
from .integrations import (find_least_compatible_version, get_integration_schema_fields,
                           load_integrations_manifests, load_integrations_schemas,
                           parse_datasets)
from .mixins import MarshmallowDataclassMixin, StackCompatMixin
from .rule_formatter import nested_normalize, toml_write
from .schemas import (SCHEMA_DIR, definitions, downgrade,
                      get_min_supported_stack_version, get_stack_schemas,
                      strip_non_public_fields)
from .schemas.stack_compat import get_restricted_fields
from .utils import PatchedTemplate, cached, convert_time_span, get_nested_value, set_nested_value


_META_SCHEMA_REQ_DEFAULTS = {}
MIN_FLEET_PACKAGE_VERSION = '7.13.0'
TIME_NOW = time.strftime('%Y/%m/%d')
RULES_CONFIG = parse_rules_config()
DEFAULT_PREBUILT_RULES_DIRS = RULES_CONFIG.rule_dirs
DEFAULT_PREBUILT_BBR_DIRS = RULES_CONFIG.bbr_rules_dirs
BYPASS_VERSION_LOCK = RULES_CONFIG.bypass_version_lock


BUILD_FIELD_VERSIONS = {
    "related_integrations": (Version.parse('8.3.0'), None),
    "required_fields": (Version.parse('8.3.0'), None),
    "setup": (Version.parse('8.3.0'), None)
}


@dataclass
class DictRule:
    """Simple object wrapper for raw rule dicts."""

    contents: dict
    path: Optional[Path] = None

    @property
    def metadata(self) -> dict:
        """Metadata portion of TOML file rule."""
        return self.contents.get('metadata', {})

    @property
    def data(self) -> dict:
        """Rule portion of TOML file rule."""
        return self.contents.get('data') or self.contents

    @property
    def id(self) -> str:
        """Get the rule ID."""
        return self.data['rule_id']

    @property
    def name(self) -> str:
        """Get the rule name."""
        return self.data['name']

    def __hash__(self) -> int:
        """Get the hash of the rule."""
        return hash(self.id + self.name)

    def __repr__(self) -> str:
        """Get a string representation of the rule."""
        return f"Rule({self.name} {self.id})"


@dataclass(frozen=True)
class RuleMeta(MarshmallowDataclassMixin):
    """Data stored in a rule's [metadata] section of TOML."""
    creation_date: definitions.Date
    updated_date: definitions.Date
    deprecation_date: Optional[definitions.Date]

    # Optional fields
    bypass_bbr_timing: Optional[bool]
    comments: Optional[str]
    integration: Optional[Union[str, List[str]]]
    maturity: Optional[definitions.Maturity]
    min_stack_version: Optional[definitions.SemVer]
    min_stack_comments: Optional[str]
    os_type_list: Optional[List[definitions.OSType]]
    query_schema_validation: Optional[bool]
    related_endpoint_rules: Optional[List[str]]
    promotion: Optional[bool]

    # Extended information as an arbitrary dictionary
    extended: Optional[Dict[str, Any]]

    def get_validation_stack_versions(self) -> Dict[str, dict]:
        """Get a dict of beats and ecs versions per stack release."""
        stack_versions = get_stack_schemas(self.min_stack_version)
        return stack_versions


@dataclass(frozen=True)
class RuleTransform(MarshmallowDataclassMixin):
    """Data stored in a rule's [transform] section of TOML."""

    # note (investigation guides) Markdown plugins
    # /elastic/kibana/tree/main/x-pack/plugins/security_solution/public/common/components/markdown_editor/plugins
    ##############################################

    # timelines out of scope at the moment

    @dataclass(frozen=True)
    class OsQuery:
        label: str
        query: str
        ecs_mapping: Optional[Dict[str, Dict[Literal['field', 'value'], str]]]

    @dataclass(frozen=True)
    class Investigate:
        @dataclass(frozen=True)
        class Provider:
            excluded: bool
            field: str
            queryType: definitions.InvestigateProviderQueryType
            value: str
            valueType: definitions.InvestigateProviderValueType

        label: str
        description: Optional[str]
        providers: List[List[Provider]]
        relativeFrom: Optional[str]
        relativeTo: Optional[str]

    # these must be lists in order to have more than one. Their index in the list is how they will be referenced in the
    # note string templates
    osquery: Optional[List[OsQuery]]
    investigate: Optional[List[Investigate]]

    def render_investigate_osquery_to_string(self) -> Dict[definitions.TransformTypes, List[str]]:
        obj = self.to_dict()

        rendered: Dict[definitions.TransformTypes, List[str]] = {'osquery': [], 'investigate': []}
        for plugin, entries in obj.items():
            for entry in entries:
                rendered[plugin].append(f'!{{{plugin}{json.dumps(entry, sort_keys=True, separators=(",", ":"))}}}')

        return rendered

    ##############################################


@dataclass(frozen=True)
class BaseThreatEntry:
    id: str
    name: str
    reference: str

    @pre_load
    def modify_url(self, data: Dict[str, Any], **kwargs):
        """Modify the URL to support MITRE ATT&CK URLS with and without trailing forward slash."""
        if urlparse(data["reference"]).scheme:
            if not data["reference"].endswith("/"):
                data["reference"] += "/"
        return data


@dataclass(frozen=True)
class SubTechnique(BaseThreatEntry):
    """Mapping to threat subtechnique."""
    reference: definitions.SubTechniqueURL


@dataclass(frozen=True)
class Technique(BaseThreatEntry):
    """Mapping to threat subtechnique."""
    # subtechniques are stored at threat[].technique.subtechnique[]
    reference: definitions.TechniqueURL
    subtechnique: Optional[List[SubTechnique]]


@dataclass(frozen=True)
class Tactic(BaseThreatEntry):
    """Mapping to a threat tactic."""
    reference: definitions.TacticURL


@dataclass(frozen=True)
class ThreatMapping(MarshmallowDataclassMixin):
    """Mapping to a threat framework."""
    framework: Literal["MITRE ATT&CK"]
    tactic: Tactic
    technique: Optional[List[Technique]]

    @staticmethod
    def flatten(threat_mappings: Optional[List]) -> 'FlatThreatMapping':
        """Get flat lists of tactic and technique info."""
        tactic_names = []
        tactic_ids = []
        technique_ids = set()
        technique_names = set()
        sub_technique_ids = set()
        sub_technique_names = set()

        for entry in (threat_mappings or []):
            tactic_names.append(entry.tactic.name)
            tactic_ids.append(entry.tactic.id)

            for technique in (entry.technique or []):
                technique_names.add(technique.name)
                technique_ids.add(technique.id)

                for subtechnique in (technique.subtechnique or []):
                    sub_technique_ids.add(subtechnique.id)
                    sub_technique_names.add(subtechnique.name)

        return FlatThreatMapping(
            tactic_names=sorted(tactic_names),
            tactic_ids=sorted(tactic_ids),
            technique_names=sorted(technique_names),
            technique_ids=sorted(technique_ids),
            sub_technique_names=sorted(sub_technique_names),
            sub_technique_ids=sorted(sub_technique_ids)
        )


@dataclass(frozen=True)
class RiskScoreMapping(MarshmallowDataclassMixin):
    field: str
    operator: Optional[definitions.Operator]
    value: Optional[str]


@dataclass(frozen=True)
class SeverityMapping(MarshmallowDataclassMixin):
    field: str
    operator: Optional[definitions.Operator]
    value: Optional[str]
    severity: Optional[str]


@dataclass(frozen=True)
class FlatThreatMapping(MarshmallowDataclassMixin):
    tactic_names: List[str]
    tactic_ids: List[str]
    technique_names: List[str]
    technique_ids: List[str]
    sub_technique_names: List[str]
    sub_technique_ids: List[str]


@dataclass(frozen=True)
class AlertSuppressionDuration:
    """Mapping to alert suppression duration."""
    unit: definitions.TimeUnits
    value: definitions.AlertSuppressionValue


@dataclass(frozen=True)
class AlertSuppressionMapping(MarshmallowDataclassMixin, StackCompatMixin):
    """Mapping to alert suppression."""

    group_by: definitions.AlertSuppressionGroupBy
    duration: Optional[AlertSuppressionDuration]
    missing_fields_strategy: definitions.AlertSuppressionMissing


@dataclass(frozen=True)
class ThresholdAlertSuppression:
    """Mapping to alert suppression."""

    duration: AlertSuppressionDuration


@dataclass(frozen=True)
class FilterStateStore:
    store: definitions.StoreType


@dataclass(frozen=True)
class FilterMeta:
    alias: Optional[Union[str, None]] = None
    disabled: Optional[bool] = None
    negate: Optional[bool] = None
    controlledBy: Optional[str] = None  # identify who owns the filter
    group: Optional[str] = None  # allows grouping of filters
    index: Optional[str] = None
    isMultiIndex: Optional[bool] = None
    type: Optional[str] = None
    key: Optional[str] = None
    params: Optional[str] = None  # Expand to FilterMetaParams when needed
    value: Optional[str] = None


@dataclass(frozen=True)
class WildcardQuery:
    case_insensitive: bool
    value: str


@dataclass(frozen=True)
class Query:
    wildcard: Optional[Dict[str, WildcardQuery]] = None


@dataclass(frozen=True)
class Filter:
    """Kibana Filter for Base Rule Data."""
    # TODO: Currently unused in BaseRuleData. Revisit to extend or remove.
    # https://github.com/elastic/detection-rules/issues/3773
    meta: FilterMeta
    state: Optional[FilterStateStore] = field(metadata=dict(data_key="$state"))
    query: Optional[Union[Query, Dict[str, Any]]] = None


@dataclass(frozen=True)
class BaseRuleData(MarshmallowDataclassMixin, StackCompatMixin):
    """Base rule data."""

    @dataclass
    class InvestigationFields:
        field_names: List[definitions.NonEmptyStr]

    @dataclass
    class RequiredFields:
        name: definitions.NonEmptyStr
        type: definitions.NonEmptyStr
        ecs: bool

    @dataclass
    class RelatedIntegrations:
        package: definitions.NonEmptyStr
        version: definitions.NonEmptyStr
        integration: Optional[definitions.NonEmptyStr]

    actions: Optional[list]
    author: List[str]
    building_block_type: Optional[definitions.BuildingBlockType]
    description: str
    enabled: Optional[bool]
    exceptions_list: Optional[list]
    license: Optional[str]
    false_positives: Optional[List[str]]
    filters: Optional[List[dict]]
    # trailing `_` required since `from` is a reserved word in python
    from_: Optional[str] = field(metadata=dict(data_key="from"))
    interval: Optional[definitions.Interval]
    investigation_fields: Optional[InvestigationFields] = field(metadata=dict(metadata=dict(min_compat="8.11")))
    max_signals: Optional[definitions.MaxSignals]
    meta: Optional[Dict[str, Any]]
    name: definitions.RuleName
    note: Optional[definitions.Markdown]
    # can we remove this comment?
    # explicitly NOT allowed!
    # output_index: Optional[str]
    references: Optional[List[str]]
    related_integrations: Optional[List[RelatedIntegrations]] = field(metadata=dict(metadata=dict(min_compat="8.3")))
    required_fields: Optional[List[RequiredFields]] = field(metadata=dict(metadata=dict(min_compat="8.3")))
    revision: Optional[int] = field(metadata=dict(metadata=dict(min_compat="8.8")))
    risk_score: definitions.RiskScore
    risk_score_mapping: Optional[List[RiskScoreMapping]]
    rule_id: definitions.UUIDString
    rule_name_override: Optional[str]
    setup: Optional[definitions.Markdown] = field(metadata=dict(metadata=dict(min_compat="8.3")))
    severity_mapping: Optional[List[SeverityMapping]]
    severity: definitions.Severity
    tags: Optional[List[str]]
    throttle: Optional[str]
    timeline_id: Optional[definitions.TimelineTemplateId]
    timeline_title: Optional[definitions.TimelineTemplateTitle]
    timestamp_override: Optional[str]
    to: Optional[str]
    type: definitions.RuleType
    threat: Optional[List[ThreatMapping]]
    version: Optional[definitions.PositiveInteger]

    @classmethod
    def save_schema(cls):
        """Save the schema as a jsonschema."""
        fields: Tuple[dataclasses.Field, ...] = dataclasses.fields(cls)
        type_field = next(f for f in fields if f.name == "type")
        rule_type = typing.get_args(type_field.type)[0] if cls != BaseRuleData else "base"
        schema = cls.jsonschema()
        version_dir = SCHEMA_DIR / "master"
        version_dir.mkdir(exist_ok=True, parents=True)

        # expand out the jsonschema definitions
        with (version_dir / f"master.{rule_type}.json").open("w") as f:
            json.dump(schema, f, indent=2, sort_keys=True)

    def validate_query(self, meta: RuleMeta) -> None:
        pass

    @cached_property
    def get_restricted_fields(self) -> Optional[Dict[str, tuple]]:
        """Get stack version restricted fields."""
        fields: List[dataclasses.Field, ...] = list(dataclasses.fields(self))
        return get_restricted_fields(fields)

    @cached_property
    def data_validator(self) -> Optional['DataValidator']:
        return DataValidator(is_elastic_rule=self.is_elastic_rule, **self.to_dict())

    @cached_property
    def notify(self) -> bool:
        return os.environ.get('DR_NOTIFY_INTEGRATION_UPDATE_AVAILABLE') is not None

    @cached_property
    def parsed_note(self) -> Optional[MarkoDocument]:
        dv = self.data_validator
        if dv:
            return dv.parsed_note

    @property
    def is_elastic_rule(self):
        return 'elastic' in [a.lower() for a in self.author]

    def get_build_fields(self) -> {}:
        """Get a list of build-time fields along with the stack versions which they will build within."""
        build_fields = {}
        rule_fields = {f.name: f for f in dataclasses.fields(self)}

        for fld in BUILD_FIELD_VERSIONS:
            if fld in rule_fields:
                build_fields[fld] = BUILD_FIELD_VERSIONS[fld]

        return build_fields

    @classmethod
    def process_transforms(cls, transform: RuleTransform, obj: dict) -> dict:
        """Process transforms from toml [transform] called in TOMLRuleContents.to_dict."""
        # only create functions that CAREFULLY mutate the obj dict

        def process_note_plugins():
            """Format the note field with osquery and investigate plugin strings."""
            note = obj.get('note')
            if not note:
                return

            rendered = transform.render_investigate_osquery_to_string()
            rendered_patterns = {}
            for plugin, entries in rendered.items():
                rendered_patterns.update(**{f'{plugin}_{i}': e for i, e in enumerate(entries)})

            note_template = PatchedTemplate(note)
            rendered_note = note_template.safe_substitute(**rendered_patterns)
            obj['note'] = rendered_note

        # call transform functions
        if transform:
            process_note_plugins()

        return obj

    @validates_schema
    def validates_data(self, data, **kwargs):
        """Validate fields and data for marshmallow schemas."""

        # Validate version and revision fields not supplied.
        disallowed_fields = [field for field in ['version', 'revision'] if data.get(field) is not None]
        if not disallowed_fields:
            return

        error_message = " and ".join(disallowed_fields)

        # If version and revision fields are supplied, and using locked versions raise an error.
        if BYPASS_VERSION_LOCK is not True:
            msg = (f"Configuration error: Rule {data['name']} - {data['rule_id']} "
                   f"should not contain rules with `{error_message}` set.")
            raise ValidationError(msg)


class DataValidator:
    """Additional validation beyond base marshmallow schema validation."""

    def __init__(self,
                 name: definitions.RuleName,
                 is_elastic_rule: bool,
                 note: Optional[definitions.Markdown] = None,
                 interval: Optional[definitions.Interval] = None,
                 building_block_type: Optional[definitions.BuildingBlockType] = None,
                 setup: Optional[str] = None,
                 **extras):
        # only define fields needing additional validation
        self.name = name
        self.is_elastic_rule = is_elastic_rule
        self.note = note
        # Need to use extras because from is a reserved word in python
        self.from_ = extras.get('from')
        self.interval = interval
        self.building_block_type = building_block_type
        self.setup = setup
        self._setup_in_note = False

    @cached_property
    def parsed_note(self) -> Optional[MarkoDocument]:
        if self.note:
            return gfm.parse(self.note)

    @property
    def setup_in_note(self):
        return self._setup_in_note

    @setup_in_note.setter
    def setup_in_note(self, value: bool):
        self._setup_in_note = value

    @cached_property
    def skip_validate_note(self) -> bool:
        return os.environ.get('DR_BYPASS_NOTE_VALIDATION_AND_PARSE') is not None

    @cached_property
    def skip_validate_bbr(self) -> bool:
        return os.environ.get('DR_BYPASS_BBR_LOOKBACK_VALIDATION') is not None

    def validate_bbr(self, bypass: bool = False):
        """Validate building block type and rule type."""

        if self.skip_validate_bbr or bypass:
            return

        def validate_lookback(str_time: str) -> bool:
            """Validate that the time is at least now-119m and at least 60m respectively."""
            try:
                if "now-" in str_time:
                    str_time = str_time[4:]
                    time = convert_time_span(str_time)
                    # if from time is less than 119m as milliseconds
                    if time < 119 * 60 * 1000:
                        return False
                else:
                    return False
            except Exception as e:
                raise ValidationError(f"Invalid time format: {e}")
            return True

        def validate_interval(str_time: str) -> bool:
            """Validate that the time is at least now-119m and at least 60m respectively."""
            try:
                time = convert_time_span(str_time)
                # if interval time is less than 60m as milliseconds
                if time < 60 * 60 * 1000:
                    return False
            except Exception as e:
                raise ValidationError(f"Invalid time format: {e}")
            return True

        bypass_instructions = "To bypass, use the environment variable `DR_BYPASS_BBR_LOOKBACK_VALIDATION`"
        if self.building_block_type:
            if not self.from_ or not self.interval:
                raise ValidationError(
                    f"{self.name} is invalid."
                    "BBR require `from` and `interval` to be defined. "
                    "Please set or bypass." + bypass_instructions
                )
            elif not validate_lookback(self.from_) or not validate_interval(self.interval):
                raise ValidationError(
                    f"{self.name} is invalid."
                    "Default BBR require `from` and `interval` to be at least now-119m and at least 60m respectively "
                    "(using the now-Xm and Xm format where x is in minutes). "
                    "Please update values or bypass. " + bypass_instructions
                )

    def validate_note(self):
        if self.skip_validate_note or not self.note:
            return

        try:
            for child in self.parsed_note.children:
                if child.get_type() == "Heading":
                    header = gfm.renderer.render_children(child)

                    if header.lower() == "setup":

                        # check that the Setup header is correctly formatted at level 2
                        if child.level != 2:
                            raise ValidationError(f"Setup section with wrong header level: {child.level}")

                        # check that the Setup header is capitalized
                        if child.level == 2 and header != "Setup":
                            raise ValidationError(f"Setup header has improper casing: {header}")

                        self.setup_in_note = True

                    else:
                        # check that the header Config does not exist in the Setup section
                        if child.level == 2 and "config" in header.lower():
                            raise ValidationError(f"Setup header contains Config: {header}")

        except Exception as e:
            raise ValidationError(f"Invalid markdown in rule `{self.name}`: {e}. To bypass validation on the `note`"
                                  f"field, use the environment variable `DR_BYPASS_NOTE_VALIDATION_AND_PARSE`")

        # raise if setup header is in note and in setup
        if self.setup_in_note and (self.setup and self.setup != "None"):
            raise ValidationError("Setup header found in both note and setup fields.")


@dataclass
class QueryValidator:
    query: str

    @property
    def ast(self) -> Any:
        raise NotImplementedError()

    @property
    def unique_fields(self) -> Any:
        raise NotImplementedError()

    def validate(self, data: 'QueryRuleData', meta: RuleMeta) -> None:
        raise NotImplementedError()

    @cached
    def get_required_fields(self, index: str) -> List[Optional[dict]]:
        """Retrieves fields needed for the query along with type information from the schema."""
        if isinstance(self, ESQLValidator):
            return []

        current_version = Version.parse(load_current_package_version(), optional_minor_and_patch=True)
        ecs_version = get_stack_schemas()[str(current_version)]['ecs']
        beats_version = get_stack_schemas()[str(current_version)]['beats']
        endgame_version = get_stack_schemas()[str(current_version)]['endgame']
        ecs_schema = ecs.get_schema(ecs_version)

        beat_types, beat_schema, schema = self.get_beats_schema(index or [], beats_version, ecs_version)
        endgame_schema = self.get_endgame_schema(index or [], endgame_version)

        # construct integration schemas
        packages_manifest = load_integrations_manifests()
        integrations_schemas = load_integrations_schemas()
        datasets, _ = beats.get_datasets_and_modules(self.ast)
        package_integrations = parse_datasets(datasets, packages_manifest)
        int_schema = {}
        data = {"notify": False}

        for pk_int in package_integrations:
            package = pk_int["package"]
            integration = pk_int["integration"]
            schema, _ = get_integration_schema_fields(integrations_schemas, package, integration,
                                                      current_version, packages_manifest, {}, data)
            int_schema.update(schema)

        required = []
        unique_fields = self.unique_fields or []

        for fld in unique_fields:
            field_type = ecs_schema.get(fld, {}).get('type')
            is_ecs = field_type is not None

            if not is_ecs:
                if int_schema:
                    field_type = int_schema.get(fld, None)
                elif beat_schema:
                    field_type = beat_schema.get(fld, {}).get('type')
                elif endgame_schema:
                    field_type = endgame_schema.endgame_schema.get(fld, None)

            required.append(dict(name=fld, type=field_type or 'unknown', ecs=is_ecs))

        return sorted(required, key=lambda f: f['name'])

    @cached
    def get_beats_schema(self, index: list, beats_version: str, ecs_version: str) -> (list, dict, dict):
        """Get an assembled beats schema."""
        beat_types = beats.parse_beats_from_index(index)
        beat_schema = beats.get_schema_from_kql(self.ast, beat_types, version=beats_version) if beat_types else None
        schema = ecs.get_kql_schema(version=ecs_version, indexes=index, beat_schema=beat_schema)
        return beat_types, beat_schema, schema

    @cached
    def get_endgame_schema(self, index: list, endgame_version: str) -> Optional[endgame.EndgameSchema]:
        """Get an assembled flat endgame schema."""

        if index and "endgame-*" not in index:
            return None

        endgame_schema = endgame.read_endgame_schema(endgame_version=endgame_version)
        return endgame.EndgameSchema(endgame_schema)


@dataclass(frozen=True)
class QueryRuleData(BaseRuleData):
    """Specific fields for query event types."""
    type: Literal["query"]

    index: Optional[List[str]]
    data_view_id: Optional[str]
    query: str
    language: definitions.FilterLanguages
    alert_suppression: Optional[AlertSuppressionMapping] = field(metadata=dict(metadata=dict(min_compat="8.8")))

    @cached_property
    def index_or_dataview(self) -> list[str]:
        """Return the index or dataview depending on which is set. If neither returns empty list."""
        if self.index is not None:
            return self.index
        elif self.data_view_id is not None:
            return [self.data_view_id]
        else:
            return []

    @cached_property
    def validator(self) -> Optional[QueryValidator]:
        if self.language == "kuery":
            return KQLValidator(self.query)
        elif self.language == "eql":
            return EQLValidator(self.query)
        elif self.language == "esql":
            return ESQLValidator(self.query)

    def validate_query(self, meta: RuleMeta) -> None:
        validator = self.validator
        if validator is not None:
            return validator.validate(self, meta)

    @cached_property
    def ast(self):
        validator = self.validator
        if validator is not None:
            return validator.ast

    @cached_property
    def unique_fields(self):
        validator = self.validator
        if validator is not None:
            return validator.unique_fields

    @cached
    def get_required_fields(self, index: str) -> List[dict]:
        validator = self.validator
        if validator is not None:
            return validator.get_required_fields(index or [])

    @validates_schema
    def validates_index_and_data_view_id(self, data, **kwargs):
        """Validate that either index or data_view_id is set, but not both."""
        if data.get('index') and data.get('data_view_id'):
            raise ValidationError("Only one of index or data_view_id should be set.")


@dataclass(frozen=True)
class MachineLearningRuleData(BaseRuleData):
    type: Literal["machine_learning"]

    anomaly_threshold: int
    machine_learning_job_id: Union[str, List[str]]
    alert_suppression: Optional[AlertSuppressionMapping] = field(metadata=dict(metadata=dict(min_compat="8.15")))


@dataclass(frozen=True)
class ThresholdQueryRuleData(QueryRuleData):
    """Specific fields for query event types."""

    @dataclass(frozen=True)
    class ThresholdMapping(MarshmallowDataclassMixin):
        @dataclass(frozen=True)
        class ThresholdCardinality:
            field: str
            value: definitions.ThresholdValue

        field: definitions.CardinalityFields
        value: definitions.ThresholdValue
        cardinality: Optional[List[ThresholdCardinality]]

    type: Literal["threshold"]
    threshold: ThresholdMapping
    alert_suppression: Optional[ThresholdAlertSuppression] = field(metadata=dict(metadata=dict(min_compat="8.12")))


@dataclass(frozen=True)
class NewTermsRuleData(QueryRuleData):
    """Specific fields for new terms field rule."""

    @dataclass(frozen=True)
    class NewTermsMapping(MarshmallowDataclassMixin):
        @dataclass(frozen=True)
        class HistoryWindowStart:
            field: definitions.NonEmptyStr
            value: definitions.NonEmptyStr

        field: definitions.NonEmptyStr
        value: definitions.NewTermsFields
        history_window_start: List[HistoryWindowStart]

    type: Literal["new_terms"]
    new_terms: NewTermsMapping
    alert_suppression: Optional[AlertSuppressionMapping] = field(metadata=dict(metadata=dict(min_compat="8.14")))

    @pre_load
    def preload_data(self, data: dict, **kwargs) -> dict:
        """Preloads and formats the data to match the required schema."""
        if "new_terms_fields" in data and "history_window_start" in data:
            new_terms_mapping = {
                "field": "new_terms_fields",
                "value": data["new_terms_fields"],
                "history_window_start": [
                    {
                        "field": "history_window_start",
                        "value": data["history_window_start"]
                    }
                ]
            }
            data["new_terms"] = new_terms_mapping

            # cleanup original fields after building into our toml format
            data.pop("new_terms_fields")
            data.pop("history_window_start")
        return data

    def transform(self, obj: dict) -> dict:
        """Transforms new terms data to API format for Kibana."""
        obj[obj["new_terms"].get("field")] = obj["new_terms"].get("value")
        obj["history_window_start"] = obj["new_terms"]["history_window_start"][0].get("value")
        del obj["new_terms"]
        return obj


@dataclass(frozen=True)
class EQLRuleData(QueryRuleData):
    """EQL rules are a special case of query rules."""
    type: Literal["eql"]
    language: Literal["eql"]
    timestamp_field: Optional[str] = field(metadata=dict(metadata=dict(min_compat="8.0")))
    event_category_override: Optional[str] = field(metadata=dict(metadata=dict(min_compat="8.0")))
    tiebreaker_field: Optional[str] = field(metadata=dict(metadata=dict(min_compat="8.0")))
    alert_suppression: Optional[AlertSuppressionMapping] = field(metadata=dict(metadata=dict(min_compat="8.14")))

    def convert_relative_delta(self, lookback: str) -> int:
        now = len("now")
        min_length = now + len('+5m')

        if lookback.startswith("now") and len(lookback) >= min_length:
            lookback = lookback[len("now"):]
            sign = lookback[0]  # + or -
            span = lookback[1:]
            amount = convert_time_span(span)
            return amount * (-1 if sign == "-" else 1)
        else:
            return convert_time_span(lookback)

    @cached_property
    def is_sample(self) -> bool:
        """Checks if the current rule is a sample-based rule."""
        return eql.utils.get_query_type(self.ast) == 'sample'

    @cached_property
    def is_sequence(self) -> bool:
        """Checks if the current rule is a sequence-based rule."""
        return eql.utils.get_query_type(self.ast) == 'sequence'

    @cached_property
    def max_span(self) -> Optional[int]:
        """Maxspan value for sequence rules if defined."""
        if self.is_sequence and hasattr(self.ast.first, 'max_span'):
            return self.ast.first.max_span.as_milliseconds() if self.ast.first.max_span else None

    @cached_property
    def look_back(self) -> Optional[Union[int, Literal['unknown']]]:
        """Lookback value of a rule."""
        # https://www.elastic.co/guide/en/elasticsearch/reference/current/common-options.html#date-math
        to = self.convert_relative_delta(self.to) if self.to else 0
        from_ = self.convert_relative_delta(self.from_ or "now-6m")

        if not (to or from_):
            return 'unknown'
        else:
            return to - from_

    @cached_property
    def interval_ratio(self) -> Optional[float]:
        """Ratio of interval time window / max_span time window."""
        if self.max_span:
            interval = convert_time_span(self.interval or '5m')
            return interval / self.max_span


@dataclass(frozen=True)
class ESQLRuleData(QueryRuleData):
    """ESQL rules are a special case of query rules."""
    type: Literal["esql"]
    language: Literal["esql"]
    query: str
    alert_suppression: Optional[AlertSuppressionMapping] = field(metadata=dict(metadata=dict(min_compat="8.15")))

    @validates_schema
    def validates_esql_data(self, data, **kwargs):
        """Custom validation for query rule type and subclasses."""
        if data.get('index'):
            raise ValidationError("Index is not a valid field for ES|QL rule type.")

        # Convert the query string to lowercase to handle case insensitivity
        query_lower = data['query'].lower()

        # Combine both patterns using an OR operator and compile the regex
        combined_pattern = re.compile(
            r'(from\s+\S+\s+metadata\s+_id,\s*_version,\s*_index)|(\bstats\b.*?\bby\b)', re.DOTALL
        )

        # Ensure that non-aggregate queries have metadata
        if not combined_pattern.search(query_lower):
            raise ValidationError(
                f"Rule: {data['name']} contains a non-aggregate query without"
                f" metadata fields '_id', '_version', and '_index' ->"
                f" Add 'metadata _id, _version, _index' to the from command or add an aggregate function."
            )

        # Enforce KEEP command for ESQL rules
        if '| keep' not in query_lower:
            raise ValidationError(
                f"Rule: {data['name']} does not contain a 'keep' command ->"
                f" Add a 'keep' command to the query."
            )


@dataclass(frozen=True)
class ThreatMatchRuleData(QueryRuleData):
    """Specific fields for indicator (threat) match rule."""

    @dataclass(frozen=True)
    class Entries:

        @dataclass(frozen=True)
        class ThreatMapEntry:
            field: definitions.NonEmptyStr
            type: Literal["mapping"]
            value: definitions.NonEmptyStr

        entries: List[ThreatMapEntry]

    type: Literal["threat_match"]

    concurrent_searches: Optional[definitions.PositiveInteger]
    items_per_search: Optional[definitions.PositiveInteger]

    threat_mapping: List[Entries]
    threat_filters: Optional[List[dict]]
    threat_query: Optional[str]
    threat_language: Optional[definitions.FilterLanguages]
    threat_index: List[str]
    threat_indicator_path: Optional[str]
    alert_suppression: Optional[AlertSuppressionMapping] = field(metadata=dict(metadata=dict(min_compat="8.13")))

    def validate_query(self, meta: RuleMeta) -> None:
        super(ThreatMatchRuleData, self).validate_query(meta)

        if self.threat_query:
            if not self.threat_language:
                raise ValidationError('`threat_language` required when a `threat_query` is defined')

            if self.threat_language == "kuery":
                threat_query_validator = KQLValidator(self.threat_query)
            elif self.threat_language == "eql":
                threat_query_validator = EQLValidator(self.threat_query)
            else:
                return

            threat_query_validator.validate(self, meta)


# All of the possible rule types
# Sort inverse of any inheritance - see comment in TOMLRuleContents.to_dict
AnyRuleData = Union[EQLRuleData, ESQLRuleData, ThresholdQueryRuleData, ThreatMatchRuleData,
                    MachineLearningRuleData, QueryRuleData, NewTermsRuleData]


class BaseRuleContents(ABC):
    """Base contents object for shared methods between active and deprecated rules."""

    @property
    @abstractmethod
    def id(self):
        pass

    @property
    @abstractmethod
    def name(self):
        pass

    @property
    @abstractmethod
    def version_lock(self):
        pass

    @property
    @abstractmethod
    def type(self):
        pass

    def lock_info(self, bump=True) -> dict:
        version = self.autobumped_version if bump else (self.saved_version or 1)
        contents = {"rule_name": self.name, "sha256": self.get_hash(), "version": version, "type": self.type}

        return contents

    @property
    def is_dirty(self) -> bool:
        """Determine if the rule has changed since its version was locked."""
        min_stack = Version.parse(self.get_supported_version(), optional_minor_and_patch=True)
        existing_sha256 = self.version_lock.get_locked_hash(self.id, f"{min_stack.major}.{min_stack.minor}")

        if not existing_sha256:
            return False

        rule_hash = self.get_hash()
        rule_hash_with_integrations = self.get_hash(include_integrations=True)

        # Checking against current and previous version of the hash to avoid mass version bump
        is_dirty = existing_sha256 not in (rule_hash, rule_hash_with_integrations)
        return is_dirty

    @property
    def lock_entry(self) -> Optional[dict]:
        lock_entry = self.version_lock.version_lock.data.get(self.id)
        if lock_entry:
            return lock_entry.to_dict()

    @property
    def has_forked(self) -> bool:
        """Determine if the rule has forked at any point (has a previous entry)."""
        lock_entry = self.lock_entry
        if lock_entry:
            return 'previous' in lock_entry
        return False

    @property
    def is_in_forked_version(self) -> bool:
        """Determine if the rule is in a forked version."""
        if not self.has_forked:
            return False
        locked_min_stack = Version.parse(self.lock_entry['min_stack_version'], optional_minor_and_patch=True)
        current_package_ver = Version.parse(load_current_package_version(), optional_minor_and_patch=True)
        return current_package_ver < locked_min_stack

    def get_version_space(self) -> Optional[int]:
        """Retrieve the number of version spaces available (None for unbound)."""
        if self.is_in_forked_version:
            current_entry = self.lock_entry['previous'][self.metadata.min_stack_version]
            current_version = current_entry['version']
            max_allowable_version = current_entry['max_allowable_version']

            return max_allowable_version - current_version - 1

    @property
    def saved_version(self) -> Optional[int]:
        """Retrieve the version from the version.lock or from the file if version locking is bypassed."""
        toml_version = self.data.get("version")

        if BYPASS_VERSION_LOCK:
            return toml_version

        if toml_version:
            print(f"WARNING: Rule {self.name} - {self.id} has a version set in the rule TOML."
                  " This `version` will be ignored and defaulted to the version.lock.json file."
                  " Set `bypass_version_lock` to `True` in the rules config to use the TOML version.")

        return self.version_lock.get_locked_version(self.id, self.get_supported_version())

    @property
    def autobumped_version(self) -> Optional[int]:
        """Retrieve the current version of the rule, accounting for automatic increments."""
        version = self.saved_version

        if BYPASS_VERSION_LOCK:
            raise NotImplementedError("This method is not implemented when version locking is not in use.")

        # Default to version 1 if no version is set yet
        if version is None:
            return 1

        # Auto-increment version if the rule is 'dirty' and not bypassing version lock
        return version + 1 if self.is_dirty else version

    def get_synthetic_version(self, use_default: bool) -> Optional[int]:
        """
        Get the latest actual representation of a rule's version, where changes are accounted for automatically when
        version locking is used, otherwise, return the version defined in the rule toml if present else optionally
        default to 1.
        """
        return self.autobumped_version or self.saved_version or (1 if use_default else None)

    @classmethod
    def convert_supported_version(cls, stack_version: Optional[str]) -> Version:
        """Convert an optional stack version to the minimum for the lock in the form major.minor."""
        min_version = get_min_supported_stack_version()
        if stack_version is None:
            return min_version
        return max(Version.parse(stack_version, optional_minor_and_patch=True), min_version)

    def get_supported_version(self) -> str:
        """Get the lowest stack version for the rule that is currently supported in the form major.minor."""
        rule_min_stack = self.metadata.get('min_stack_version')
        min_stack = self.convert_supported_version(rule_min_stack)
        return f"{min_stack.major}.{min_stack.minor}"

    def _post_dict_conversion(self, obj: dict) -> dict:
        """Transform the converted API in place before sending to Kibana."""

        # cleanup the whitespace in the rule
        obj = nested_normalize(obj)

        # fill in threat.technique so it's never missing
        for threat_entry in obj.get("threat", []):
            threat_entry.setdefault("technique", [])

        return obj

    @abstractmethod
    def to_api_format(self, include_version: bool = True) -> dict:
        """Convert the rule to the API format."""

    def get_hashable_content(self, include_version: bool = False, include_integrations: bool = False) -> dict:
        """Returns the rule content to be used for calculating the hash value for the rule"""

        # get the API dict without the version by default, otherwise it'll always be dirty.
        hashable_dict = self.to_api_format(include_version=include_version)

        # drop related integrations if present
        if not include_integrations:
            hashable_dict.pop("related_integrations", None)

        return hashable_dict

    @cached
    def get_hash(self, include_version: bool = False, include_integrations: bool = False) -> str:
        """Returns a sha256 hash of the rule contents"""
        hashable_contents = self.get_hashable_content(
            include_version=include_version,
            include_integrations=include_integrations,
        )
        return utils.dict_hash(hashable_contents)


@dataclass(frozen=True)
class TOMLRuleContents(BaseRuleContents, MarshmallowDataclassMixin):
    """Rule object which maps directly to the TOML layout."""
    metadata: RuleMeta
    transform: Optional[RuleTransform]
    data: AnyRuleData = field(metadata=dict(data_key="rule"))

    @cached_property
    def version_lock(self):
        # VersionLock
        from .version_lock import loaded_version_lock

        if RULES_CONFIG.bypass_version_lock is True:
            err_msg = "Cannot access the version lock when the versioning strategy is configured to bypass the" \
                      " version lock. Set `bypass_version_lock` to `false` in the rules config to use the version lock."
            raise ValueError(err_msg)

        return getattr(self, '_version_lock', None) or loaded_version_lock

    def set_version_lock(self, value):
        from .version_lock import VersionLock

        err_msg = "Cannot set the version lock when the versioning strategy is configured to bypass the version lock." \
                  " Set `bypass_version_lock` to `false` in the rules config to use the version lock."
        assert not RULES_CONFIG.bypass_version_lock, err_msg

        if value and not isinstance(value, VersionLock):
            raise TypeError(f'version lock property must be set with VersionLock objects only. Got {type(value)}')

        # circumvent frozen class
        self.__dict__['_version_lock'] = value

    @classmethod
    def all_rule_types(cls) -> set:
        types = set()
        for subclass in typing.get_args(AnyRuleData):
            field = next(field for field in dataclasses.fields(subclass) if field.name == "type")
            types.update(typing.get_args(field.type))

        return types

    @classmethod
    def get_data_subclass(cls, rule_type: str) -> typing.Type[BaseRuleData]:
        """Get the proper subclass depending on the rule type"""
        for subclass in typing.get_args(AnyRuleData):
            field = next(field for field in dataclasses.fields(subclass) if field.name == "type")
            if (rule_type, ) == typing.get_args(field.type):
                return subclass

        raise ValueError(f"Unknown rule type {rule_type}")

    @property
    def id(self) -> definitions.UUIDString:
        return self.data.rule_id

    @property
    def name(self) -> str:
        return self.data.name

    @property
    def type(self) -> str:
        return self.data.type

    def _add_known_nulls(self, rule_dict: dict) -> dict:
        """Add known nulls to the rule."""
        # Note this is primarily as a stopgap until add support for Rule Actions
        for pair in definitions.KNOWN_NULL_ENTRIES:
            for compound_key, sub_key in pair.items():
                value = get_nested_value(rule_dict, compound_key)
                if isinstance(value, list):
                    items_to_update = [
                        item for item in value if isinstance(item, dict) and get_nested_value(item, sub_key) is None
                    ]
                    for item in items_to_update:
                        set_nested_value(item, sub_key, None)
        return rule_dict

    def _post_dict_conversion(self, obj: dict) -> dict:
        """Transform the converted API in place before sending to Kibana."""
        super()._post_dict_conversion(obj)

        # build time fields
        self._convert_add_related_integrations(obj)
        self._convert_add_required_fields(obj)
        self._convert_add_setup(obj)

        # validate new fields against the schema
        rule_type = obj['type']
        subclass = self.get_data_subclass(rule_type)
        subclass.from_dict(obj)

        # rule type transforms
        self.data.transform(obj) if hasattr(self.data, 'transform') else False

        return obj

    def _convert_add_related_integrations(self, obj: dict) -> None:
        """Add restricted field related_integrations to the obj."""
        field_name = "related_integrations"
        package_integrations = obj.get(field_name, [])

        if not package_integrations and self.metadata.integration:
            packages_manifest = load_integrations_manifests()
            current_stack_version = load_current_package_version()

            if self.check_restricted_field_version(field_name):
                if (isinstance(self.data, QueryRuleData) or isinstance(self.data, MachineLearningRuleData)):
                    if (self.data.get('language') is not None and self.data.get('language') != 'lucene') or \
                            self.data.get('type') == 'machine_learning':
                        package_integrations = self.get_packaged_integrations(self.data, self.metadata,
                                                                              packages_manifest)

                        if not package_integrations:
                            return

                        for package in package_integrations:
                            package["version"] = find_least_compatible_version(
                                package=package["package"],
                                integration=package["integration"],
                                current_stack_version=current_stack_version,
                                packages_manifest=packages_manifest)

                            # if integration is not a policy template remove
                            if package["version"]:
                                version_data = packages_manifest.get(package["package"],
                                                                     {}).get(package["version"].strip("^"), {})
                                policy_templates = version_data.get("policy_templates", [])

                                if package["integration"] not in policy_templates:
                                    del package["integration"]

                    # remove duplicate entries
                    package_integrations = list({json.dumps(d, sort_keys=True):
                                                d for d in package_integrations}.values())
                    obj.setdefault("related_integrations", package_integrations)

    def _convert_add_required_fields(self, obj: dict) -> None:
        """Add restricted field required_fields to the obj, derived from the query AST."""
        if isinstance(self.data, QueryRuleData) and self.data.language != 'lucene':
            index = obj.get('index') or []
            required_fields = self.data.get_required_fields(index)
        else:
            required_fields = []

        field_name = "required_fields"
        if required_fields and self.check_restricted_field_version(field_name=field_name):
            obj.setdefault(field_name, required_fields)

    def _convert_add_setup(self, obj: dict) -> None:
        """Add restricted field setup to the obj."""
        rule_note = obj.get("note", "")
        field_name = "setup"
        field_value = obj.get(field_name)

        if not self.check_explicit_restricted_field_version(field_name):
            return

        data_validator = self.data.data_validator

        if not data_validator.skip_validate_note and data_validator.setup_in_note and not field_value:
            parsed_note = self.data.parsed_note

            # parse note tree
            for i, child in enumerate(parsed_note.children):
                if child.get_type() == "Heading" and "Setup" in gfm.render(child):
                    field_value = self._convert_get_setup_content(parsed_note.children[i + 1:])

                    # clean up old note field
                    investigation_guide = rule_note.replace("## Setup\n\n", "")
                    investigation_guide = investigation_guide.replace(field_value, "").strip()
                    obj["note"] = investigation_guide
                    obj[field_name] = field_value
                    break

    @cached
    def _convert_get_setup_content(self, note_tree: list) -> str:
        """Get note paragraph starting from the setup header."""
        setup = []
        for child in note_tree:
            if child.get_type() == "BlankLine" or child.get_type() == "LineBreak":
                setup.append("\n")
            elif child.get_type() == "CodeSpan":
                setup.append(f"`{gfm.renderer.render_raw_text(child)}`")
            elif child.get_type() == "Paragraph":
                setup.append(self._convert_get_setup_content(child.children))
                setup.append("\n")
            elif child.get_type() == "FencedCode":
                setup.append(f"```\n{self._convert_get_setup_content(child.children)}\n```")
                setup.append("\n")
            elif child.get_type() == "RawText":
                setup.append(child.children)
            elif child.get_type() == "Heading" and child.level >= 2:
                break
            else:
                setup.append(self._convert_get_setup_content(child.children))

        return "".join(setup).strip()

    def check_explicit_restricted_field_version(self, field_name: str) -> bool:
        """Explicitly check restricted fields against global min and max versions."""
        min_stack, max_stack = BUILD_FIELD_VERSIONS[field_name]
        return self.compare_field_versions(min_stack, max_stack)

    def check_restricted_field_version(self, field_name: str) -> bool:
        """Check restricted fields against schema min and max versions."""
        min_stack, max_stack = self.data.get_restricted_fields.get(field_name)
        return self.compare_field_versions(min_stack, max_stack)

    @staticmethod
    def compare_field_versions(min_stack: Version, max_stack: Version) -> bool:
        """Check current rule version is within min and max stack versions."""
        current_version = Version.parse(load_current_package_version(), optional_minor_and_patch=True)
        max_stack = max_stack or current_version
        return min_stack <= current_version >= max_stack

    @classmethod
    def get_packaged_integrations(cls, data: QueryRuleData, meta: RuleMeta,
                                  package_manifest: dict) -> Optional[List[dict]]:
        packaged_integrations = []
        datasets, _ = beats.get_datasets_and_modules(data.get('ast') or [])

        # integration is None to remove duplicate references upstream in Kibana
        # chronologically, event.dataset is checked for package:integration, then rule tags
        # if both exist, rule tags are only used if defined in definitions for non-dataset packages
        # of machine learning analytic packages

        rule_integrations = meta.get("integration", [])
        if rule_integrations:
            for integration in rule_integrations:
                ineligible_integrations = definitions.NON_DATASET_PACKAGES + \
                    [*map(str.lower, definitions.MACHINE_LEARNING_PACKAGES)]
                if integration in ineligible_integrations or isinstance(data, MachineLearningRuleData):
                    packaged_integrations.append({"package": integration, "integration": None})

        packaged_integrations.extend(parse_datasets(datasets, package_manifest))

        return packaged_integrations

    @validates_schema
    def post_conversion_validation(self, value: dict, **kwargs):
        """Additional validations beyond base marshmallow schemas."""
        data: AnyRuleData = value["data"]
        metadata: RuleMeta = value["metadata"]

        test_config = RULES_CONFIG.test_config
        if not test_config.check_skip_by_rule_id(value['data'].rule_id):
            data.validate_query(metadata)
            data.data_validator.validate_note()
            data.data_validator.validate_bbr(metadata.get('bypass_bbr_timing'))
            data.validate(metadata) if hasattr(data, 'validate') else False

    @staticmethod
    def validate_remote(remote_validator: 'RemoteValidator', contents: 'TOMLRuleContents'):
        remote_validator.validate_rule(contents)

    @classmethod
    def from_rule_resource(
        cls, rule: dict, creation_date: str = TIME_NOW, updated_date: str = TIME_NOW, maturity: str = 'development'
    ) -> 'TOMLRuleContents':
        """Create a TOMLRuleContents from a kibana rule resource."""
        integrations = [r.get("package") for r in rule.get("related_integrations")]
        meta = {
            "creation_date": creation_date,
            "updated_date": updated_date,
            "maturity": maturity,
            "integration": integrations,
        }
        contents = cls.from_dict({'metadata': meta, 'rule': rule, 'transforms': None}, unknown=marshmallow.EXCLUDE)
        return contents

    def to_dict(self, strip_none_values=True) -> dict:
        # Load schemas directly from the data and metadata classes to avoid schema ambiguity which can
        # result from union fields which contain classes and related subclasses (AnyRuleData). See issue #1141
        metadata = self.metadata.to_dict(strip_none_values=strip_none_values)
        data = self.data.to_dict(strip_none_values=strip_none_values)
        self.data.process_transforms(self.transform, data)
        dict_obj = dict(metadata=metadata, rule=data)
        return nested_normalize(dict_obj)

    def flattened_dict(self) -> dict:
        flattened = dict()
        flattened.update(self.data.to_dict())
        flattened.update(self.metadata.to_dict())
        return flattened

    def to_api_format(self, include_version: bool = not BYPASS_VERSION_LOCK, include_metadata: bool = False) -> dict:
        """Convert the TOML rule to the API format."""
        rule_dict = self.to_dict()
        rule_dict = self._add_known_nulls(rule_dict)
        converted_data = rule_dict['rule']
        converted = self._post_dict_conversion(converted_data)

        if include_metadata:
            converted["meta"] = rule_dict['metadata']

        if include_version:
            converted["version"] = self.autobumped_version

        return converted

    def check_restricted_fields_compatibility(self) -> Dict[str, dict]:
        """Check for compatibility between restricted fields and the min_stack_version of the rule."""
        default_min_stack = get_min_supported_stack_version()
        if self.metadata.min_stack_version is not None:
            min_stack = Version.parse(self.metadata.min_stack_version, optional_minor_and_patch=True)
        else:
            min_stack = default_min_stack
        restricted = self.data.get_restricted_fields

        invalid = {}
        for _field, values in restricted.items():
            if self.data.get(_field) is not None:
                min_allowed, _ = values
                if min_stack < min_allowed:
                    invalid[_field] = {'min_stack_version': min_stack, 'min_allowed_version': min_allowed}

        return invalid


@dataclass
class TOMLRule:
    contents: TOMLRuleContents = field(hash=True)
    path: Optional[Path] = None
    gh_pr: Any = field(hash=False, compare=False, default=None, repr=False)

    @property
    def id(self):
        return self.contents.id

    @property
    def name(self):
        return self.contents.data.name

    def get_asset(self) -> dict:
        """Generate the relevant fleet compatible asset."""
        return {"id": self.id, "attributes": self.contents.to_api_format(), "type": definitions.SAVED_OBJECT_TYPE}

    def get_base_rule_dir(self) -> Path | None:
        """Get the base rule directory for the rule."""
        rule_path = self.path.resolve()
        for rules_dir in DEFAULT_PREBUILT_RULES_DIRS + DEFAULT_PREBUILT_BBR_DIRS:
            if rule_path.is_relative_to(rules_dir):
                return rule_path.relative_to(rules_dir)
        return None

    def save_toml(self, strip_none_values: bool = True):
        assert self.path is not None, f"Can't save rule {self.name} (self.id) without a path"
        converted = dict(
            metadata=self.contents.metadata.to_dict(),
            rule=self.contents.data.to_dict(strip_none_values=strip_none_values),
        )
        if self.contents.transform:
            converted["transform"] = self.contents.transform.to_dict()
        toml_write(converted, str(self.path.absolute()))

    def save_json(self, path: Path, include_version: bool = True):
        path = path.with_suffix('.json')
        with open(str(path.absolute()), 'w', newline='\n') as f:
            json.dump(self.contents.to_api_format(include_version=include_version), f, sort_keys=True, indent=2)
            f.write('\n')


@dataclass(frozen=True)
class DeprecatedRuleContents(BaseRuleContents):
    metadata: dict
    data: dict
    transform: Optional[dict]

    @cached_property
    def version_lock(self):
        # VersionLock
        from .version_lock import loaded_version_lock

        return getattr(self, '_version_lock', None) or loaded_version_lock

    def set_version_lock(self, value):
        from .version_lock import VersionLock

        err_msg = "Cannot set the version lock when the versioning strategy is configured to bypass the version lock." \
                  " Set `bypass_version_lock` to `false` in the rules config to use the version lock."
        assert not RULES_CONFIG.bypass_version_lock, err_msg

        if value and not isinstance(value, VersionLock):
            raise TypeError(f'version lock property must be set with VersionLock objects only. Got {type(value)}')

        # circumvent frozen class
        self.__dict__['_version_lock'] = value

    @property
    def id(self) -> str:
        return self.data.get('rule_id')

    @property
    def name(self) -> str:
        return self.data.get('name')

    @property
    def type(self) -> str:
        return self.data.get('type')

    @classmethod
    def from_dict(cls, obj: dict):
        kwargs = dict(metadata=obj['metadata'], data=obj['rule'])
        kwargs['transform'] = obj['transform'] if 'transform' in obj else None
        return cls(**kwargs)

    def to_api_format(self, include_version: bool = not BYPASS_VERSION_LOCK) -> dict:
        """Convert the TOML rule to the API format."""
        data = copy.deepcopy(self.data)
        if self.transform:
            transform = RuleTransform.from_dict(self.transform)
            BaseRuleData.process_transforms(transform, data)

        converted = data
        if include_version:
            converted["version"] = self.autobumped_version

        converted = self._post_dict_conversion(converted)
        return converted


class DeprecatedRule(dict):
    """Minimal dict object for deprecated rule."""

    def __init__(self, path: Path, contents: DeprecatedRuleContents, *args, **kwargs):
        super(DeprecatedRule, self).__init__(*args, **kwargs)
        self.path = path
        self.contents = contents

    def __repr__(self):
        return f'{type(self).__name__}(contents={self.contents}, path={self.path})'

    @property
    def id(self) -> str:
        return self.contents.id

    @property
    def name(self) -> str:
        return self.contents.name


def downgrade_contents_from_rule(rule: TOMLRule, target_version: str,
                                 replace_id: bool = True, include_metadata: bool = False) -> dict:
    """Generate the downgraded contents from a rule."""
    rule_dict = rule.contents.to_dict()["rule"]
    min_stack_version = target_version or rule.contents.metadata.min_stack_version or "8.3.0"
    min_stack_version = Version.parse(min_stack_version,
                                      optional_minor_and_patch=True)
    rule_dict.setdefault("meta", {}).update(rule.contents.metadata.to_dict())

    if replace_id:
        rule_dict["rule_id"] = str(uuid4())

    rule_dict = downgrade(rule_dict, target_version=str(min_stack_version))
    meta = rule_dict.pop("meta")
    rule_contents_dict = {"rule": rule_dict, "metadata": meta}

    if rule.contents.transform:
        rule_contents_dict["transform"] = rule.contents.transform.to_dict()

    rule_contents = TOMLRuleContents.from_dict(rule_contents_dict)
    payload = rule_contents.to_api_format(include_metadata=include_metadata)
    payload = strip_non_public_fields(min_stack_version, payload)
    return payload


def set_eql_config(min_stack_version: str) -> eql.parser.ParserConfig:
    """Based on the rule version set the eql functions allowed."""
    if not min_stack_version:
        min_stack_version = Version.parse(load_current_package_version(), optional_minor_and_patch=True)
    else:
        min_stack_version = Version.parse(min_stack_version, optional_minor_and_patch=True)

    config = eql.parser.ParserConfig()

    for feature, version_range in definitions.ELASTICSEARCH_EQL_FEATURES.items():
        if version_range[0] <= min_stack_version <= (version_range[1] or min_stack_version):
            config.context[feature] = True

    return config


def get_unique_query_fields(rule: TOMLRule) -> List[str]:
    """Get a list of unique fields used in a rule query from rule contents."""
    contents = rule.contents.to_api_format()
    language = contents.get('language')
    query = contents.get('query')
    if language in ('kuery', 'eql'):
        # TODO: remove once py-eql supports ipv6 for cidrmatch

        cfg = set_eql_config(rule.contents.metadata.get('min_stack_version'))
        with eql.parser.elasticsearch_syntax, eql.parser.ignore_missing_functions, eql.parser.skip_optimizations, cfg:
            parsed = (kql.parse(query, normalize_kql_keywords=RULES_CONFIG.normalize_kql_keywords)
                      if language == 'kuery' else eql.parse_query(query))
        return sorted(set(str(f) for f in parsed if isinstance(f, (eql.ast.Field, kql.ast.Field))))


# avoid a circular import
from .rule_validators import EQLValidator, ESQLValidator, KQLValidator  # noqa: E402
from .remote_validation import RemoteValidator  # noqa: E402
