"""Internal data model.

Class names and the ``ftm_schema`` tag mirror the FollowTheMoney (FtM) schema
that OpenSanctions publishes in, so data from Companies House, GLEIF and
OpenSanctions can share one vocabulary (Company / Person / LegalEntity /
Ownership) without pulling in the full FtM library at this stage.

Data minimisation: only fields needed for KYB analytics are kept. Personal
addresses and full dates of birth are never stored (PSC data only has
month/year anyway).
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

Source = Literal["companies_house", "gleif", "opensanctions", "synthetic_doc"]


class Entity(BaseModel):
    ftm_schema: ClassVar[str] = "LegalEntity"
    source: Source

    @property
    def schema_name(self) -> str:
        return self.ftm_schema


class Company(Entity):
    """A UK company from Companies House basic company data."""

    ftm_schema: ClassVar[str] = "Company"
    source: Source = "companies_house"
    company_number: str
    name: str
    status: str | None = None
    category: str | None = None
    incorporation_date: str | None = None
    country: str | None = None
    postcode: str | None = None
    sic_codes: list[str] = Field(default_factory=list)
    # PSC statements filed by the company, e.g. "no-individual-or-entity-with-signficant-control".
    psc_statements: list[str] = Field(default_factory=list)


class Person(Entity):
    """An individual PSC record (one record per company filing; NOT yet deduplicated).

    The same human can appear under many companies with different psc_ids.
    Merging them is the Entity Resolution agent's job, so it is deliberately
    not done here.
    """

    ftm_schema: ClassVar[str] = "Person"
    source: Source = "companies_house"
    psc_key: str
    name: str
    nationality: str | None = None
    country_of_residence: str | None = None
    birth_year: int | None = None
    birth_month: int | None = None


class LegalEntity(Entity):
    """A corporate/legal-person PSC that could not be resolved to a company in the subset."""

    ftm_schema: ClassVar[str] = "LegalEntity"
    source: Source = "companies_house"
    entity_key: str
    name: str
    kind: str
    registration_number: str | None = None
    country_registered: str | None = None
    legal_form: str | None = None


class Ownership(BaseModel):
    """owner --controls--> asset (FtM 'Ownership'). Shares/votes are intervals."""

    ftm_schema: ClassVar[str] = "Ownership"
    owner_label: Literal["Company", "Person", "LegalEntity", "Unknown"]
    owner_key: str
    asset_company_number: str
    psc_kind: str
    natures_of_control: list[str]
    share_min: float | None = None
    share_max: float | None = None
    vote_min: float | None = None
    vote_max: float | None = None
    appoint_directors: bool = False
    significant_influence: bool = False
    via_trust: bool = False
    via_firm: bool = False
    notified_on: str | None = None
    ceased_on: str | None = None
    # How a corporate owner was linked to a company node (registry ID, not fuzzy ER).
    resolved_by: str | None = None

    @property
    def active(self) -> bool:
        return self.ceased_on is None


class LEIRecord(BaseModel):
    ftm_schema: ClassVar[str] = "LegalEntity"
    source: Source = "gleif"
    lei: str
    name: str
    country: str | None = None
    jurisdiction: str | None = None
    entity_status: str | None = None
    registration_status: str | None = None
    ra_id: str | None = None
    ra_entity_id: str | None = None


class LEIRelationship(BaseModel):
    child_lei: str
    parent_lei: str
    relationship_type: str  # IS_DIRECTLY_CONSOLIDATED_BY / IS_ULTIMATELY_CONSOLIDATED_BY / ...
    status: str | None = None


class SanctionTarget(BaseModel):
    """A row of OpenSanctions targets.simple.csv (schema taken from the data)."""

    source: Source = "opensanctions"
    id: str
    schema_: str = Field(alias="schema")
    name: str
    aliases: list[str] = Field(default_factory=list)
    birth_date: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    identifiers: list[str] = Field(default_factory=list)
    sanctions: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}
