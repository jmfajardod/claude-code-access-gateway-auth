import pydantic
import pydantic_settings


class Settings(pydantic_settings.BaseSettings):
    model_config = pydantic_settings.SettingsConfigDict(case_sensitive=False)

    is_prod: bool
    google_client_id: str
    google_client_secret: pydantic.SecretStr
    public_base_url: str | None = None
    jwt_signing_key: pydantic.SecretStr
    storage_enc_key: pydantic.SecretStr
    ddb_table_name: str
    allowed_workspace_domains: str
    aws_region: str | None = None
    aws_default_region: str | None = None

    # Athena config for query_data_catalog. No default database — callers
    # supply fully-qualified `db.table` references (or pass `database` to
    # the tool) because LakeFormation may grant Gold-tag access across
    # multiple databases.
    athena_workgroup: str = ""
    athena_results_bucket: str = ""
    athena_query_timeout_seconds: int = 600
    athena_inline_max_bytes: int = 102_400
    athena_inline_max_rows: int = 1000
    presigned_url_ttl_seconds: int = 300

    @pydantic.computed_field
    @property
    def allowed_domains(self) -> frozenset[str]:
        return frozenset(
            d.strip().lower()
            for d in self.allowed_workspace_domains.split(",")
            if d.strip()
        )

    @pydantic.computed_field
    @property
    def aws_region_name(self) -> str | None:
        return self.aws_region or self.aws_default_region


settings = Settings()
