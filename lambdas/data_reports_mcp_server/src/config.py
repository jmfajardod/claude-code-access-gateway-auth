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
