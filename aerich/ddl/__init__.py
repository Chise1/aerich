from typing import Iterable, Tuple, Type

from tortoise import BaseDBAsyncClient
from tortoise.backends.base.schema_generator import BaseSchemaGenerator


class BaseDDL:
    schema_generator_cls: Type[BaseSchemaGenerator] = BaseSchemaGenerator
    DIALECT = "sql"
    _DROP_TABLE_TEMPLATE = 'DROP TABLE IF EXISTS "{table_name}"'
    _ADD_COLUMN_TEMPLATE = 'ALTER TABLE "{table_name}" ADD {column}'
    _DROP_COLUMN_TEMPLATE = 'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"'
    _ALTER_DEFAULT_TEMPLATE = 'ALTER TABLE "{table_name}" ALTER COLUMN "{column}" {default}'
    _RENAME_COLUMN_TEMPLATE = (
        'ALTER TABLE "{table_name}" RENAME COLUMN "{old_column_name}" TO "{new_column_name}"'
    )
    _ADD_INDEX_TEMPLATE = (
        'ALTER TABLE "{table_name}" ADD {unique}INDEX "{index_name}" ({column_names})'
    )
    _DROP_INDEX_TEMPLATE = 'ALTER TABLE "{table_name}" DROP INDEX "{index_name}"'
    _ADD_FK_TEMPLATE = 'ALTER TABLE "{table_name}" ADD CONSTRAINT "{fk_name}" FOREIGN KEY ("{db_column}") REFERENCES "{table}" ("{field}") ON DELETE {on_delete}'
    _DROP_FK_TEMPLATE = 'ALTER TABLE "{table_name}" DROP FOREIGN KEY "{fk_name}"'
    _M2M_TABLE_TEMPLATE = 'CREATE TABLE "{table_name}" ("{backward_key}" {backward_type} NOT NULL REFERENCES "{backward_table}" ("{backward_field}") ON DELETE CASCADE,"{forward_key}" {forward_type} NOT NULL REFERENCES "{forward_table}" ("{forward_field}") ON DELETE {on_delete}){extra}{comment}'
    _MODIFY_COLUMN_TEMPLATE = 'ALTER TABLE "{table_name}" MODIFY COLUMN {column}'
    _CHANGE_COLUMN_TEMPLATE = (
        'ALTER TABLE "{table_name}" CHANGE {old_column_name} {new_column_name} {new_column_type}'
    )
    _RENAME_TABLE_TEMPLATE = 'ALTER TABLE "{old_table_name}" RENAME TO "{new_table_name}"'

    def __init__(self, client: "BaseDBAsyncClient"):
        self.client = client
        self.schema_generator = self.schema_generator_cls(client)

    def create_table(self, table_describe: dict):
        return self.schema_generator._get_table_sql_v2(table_describe, True)[
            "table_creation_string"
        ]

    def drop_table(self, table_name: str):
        return self._DROP_TABLE_TEMPLATE.format(table_name=table_name)

    def create_m2m(
        self, table_describe: dict, field_describe: dict, reference_table_describe: dict
    ):
        through = field_describe.get("through")
        description = field_describe.get("description")
        reference_id = reference_table_describe.get("pk_field").get("db_column")
        db_field_types = reference_table_describe.get("pk_field").get("db_field_types")
        return self._M2M_TABLE_TEMPLATE.format(
            table_name=through,
            backward_table=table_describe["table"],
            forward_table=reference_table_describe.get("table"),
            backward_field=table_describe["pk_field"]["db_column"],
            forward_field=reference_id,
            backward_key=field_describe.get("backward_key"),
            backward_type=field_describe["backward_type"].get(self.DIALECT),
            forward_key=field_describe.get("forward_key"),
            forward_type=db_field_types.get(self.DIALECT) or db_field_types.get(""),
            on_delete=field_describe.get("on_delete"),
            extra=self.schema_generator._table_generate_extra(table=through),
            comment=self.schema_generator._table_comment_generator(
                table=through, comment=description
            )
            if description
            else "",
        )

    def drop_m2m(self, table_name: str):
        return self._DROP_TABLE_TEMPLATE.format(table_name=table_name)

    def add_column(self, table_name: str, field_describe: dict, is_pk: bool = False):
        description = field_describe.get("description")
        db_column = field_describe.get("db_column")
        db_field_types = field_describe.get("db_field_types")
        default = self.schema_generator._get_default(table_name, field_describe)

        return self._ADD_COLUMN_TEMPLATE.format(
            table_name=table_name,
            column=self.schema_generator._create_string(
                db_column=db_column,
                field_type=db_field_types.get(self.DIALECT, db_field_types.get("")),
                nullable="NOT NULL" if not field_describe.get("nullable") else "",
                unique="UNIQUE" if field_describe.get("unique") else "",
                comment=self.schema_generator._column_comment_generator(
                    table=table_name, column=db_column, comment=field_describe.get("description"),
                )
                if description
                else "",
                is_primary_key=is_pk,
                default=default,
            ),
        )

    def drop_column(self, table_name: str, column_name: str):
        return self._DROP_COLUMN_TEMPLATE.format(table_name=table_name, column_name=column_name)

    def modify_column(self, table_name: str, field_describe: dict, is_pk: bool = False):
        db_field_types = field_describe.get("db_field_types")
        default = self.schema_generator._get_default(table_name, field_describe)

        return self._MODIFY_COLUMN_TEMPLATE.format(
            table_name=table_name,
            column=self.schema_generator._create_string(
                db_column=field_describe.get("db_column"),
                field_type=db_field_types.get(self.DIALECT) or db_field_types.get(""),
                nullable="NOT NULL" if not field_describe.get("nullable") else "",
                unique="",
                comment=self.schema_generator._column_comment_generator(
                    table=table_name,
                    column=field_describe.get("db_column"),
                    comment=field_describe.get("description"),
                )
                if field_describe.get("description")
                else "",
                is_primary_key=is_pk,
                default=default,
            ),
        )

    def rename_column(self, table_name: str, old_column_name: str, new_column_name: str):
        return self._RENAME_COLUMN_TEMPLATE.format(
            table_name=table_name, old_column_name=old_column_name, new_column_name=new_column_name,
        )

    def change_column(
        self, table_name, old_column_name: str, new_column_name: str, new_column_type: str
    ):
        return self._CHANGE_COLUMN_TEMPLATE.format(
            table_name=table_name,
            old_column_name=old_column_name,
            new_column_name=new_column_name,
            new_column_type=new_column_type,
        )

    def add_index(self, table_name: str, field_names: Iterable[str], unique=False):
        return self._ADD_INDEX_TEMPLATE.format(
            unique="UNIQUE " if unique else "",
            index_name=self.schema_generator._generate_index_name(
                "idx" if not unique else "uid", table_name, field_names
            ),
            table_name=table_name,
            column_names=", ".join([self.schema_generator.quote(f) for f in field_names]),
        )

    def drop_index(self, table_name: str, field_names: Tuple[str, ...], unique=False):
        return self._DROP_INDEX_TEMPLATE.format(
            index_name=self.schema_generator._generate_index_name(
                "idx" if not unique else "uid", table_name, field_names
            ),
            table_name=table_name,
        )

    def add_fk(self, table_name: str, field_describe: dict, reference_table_describe: dict):
        db_column = field_describe.get("raw_field")
        reference_id = reference_table_describe.get("pk_field").get("db_column")
        fk_name = self.schema_generator._generate_fk_name(
            from_table=table_name,
            from_field=db_column,
            to_table=reference_table_describe.get("table"),
            to_field=reference_table_describe.get("pk_field").get("db_column"),
        )
        return self._ADD_FK_TEMPLATE.format(
            table_name=table_name,
            fk_name=fk_name,
            db_column=db_column,
            table=reference_table_describe.get("table"),
            field=reference_id,
            on_delete=field_describe.get("on_delete"),
        )

    def drop_fk(self, table_name: str, field_describe: dict, reference_table_describe: dict):
        return self._DROP_FK_TEMPLATE.format(
            table_name=table_name,
            fk_name=self.schema_generator._generate_fk_name(
                from_table=table_name,
                from_field=field_describe.get("raw_field"),
                to_table=reference_table_describe.get("table"),
                to_field=reference_table_describe.get("pk_field").get("db_column"),
            ),
        )

    def alter_column_default(self, table_name: str, field_describe: dict):
        default = self.schema_generator._get_default(table_name, field_describe)
        return self._ALTER_DEFAULT_TEMPLATE.format(
            table_name=table_name,
            column=field_describe.get("db_column"),
            default="SET" + default if default is not None else "DROP DEFAULT",
        )

    def alter_column_null(self, table_name: str, field_describe: dict):
        return self.modify_column(table_name, field_describe)

    def set_comment(self, table_name: str, field_describe: dict):
        return self.modify_column(table_name, field_describe)

    def rename_table(self, old_table_name: str, new_table_name: str):
        return self._RENAME_TABLE_TEMPLATE.format(
            old_table_name=old_table_name, new_table_name=new_table_name
        )
