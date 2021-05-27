import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Type

import click
from dictdiffer import diff
from tortoise import BaseDBAsyncClient, Model, Tortoise
from tortoise.exceptions import OperationalError

from aerich.ddl import BaseDDL
from aerich.models import MAX_VERSION_LENGTH, Aerich
from aerich.utils import (
    get_app_connection,
    get_models_describe,
    is_default_function,
    write_version_file,
)


class Migrate:
    upgrade_operators: List[str] = []
    downgrade_operators: List[str] = []
    _upgrade_fk_m2m_index_operators: List[str] = []
    _downgrade_fk_m2m_index_operators: List[str] = []
    _upgrade_m2m: List[str] = []
    _downgrade_m2m: List[str] = []
    _aerich = Aerich.__name__
    _rename_old = []
    _rename_new = []
    _modified_fields = {}
    ddl: BaseDDL
    _last_version_content: Optional[dict] = None
    app: str
    migrate_location: str
    dialect: str
    _db_version: Optional[str] = None

    @classmethod
    def get_all_version_files(cls) -> List[str]:
        return sorted(
            filter(lambda x: x.endswith("sql"), os.listdir(cls.migrate_location)),
            key=lambda x: int(x.split("_")[0]),
        )

    @classmethod
    def _get_model(cls, model: str) -> Type[Model]:
        return Tortoise.apps.get(cls.app).get(model)

    @classmethod
    async def get_last_version(cls) -> Optional[Aerich]:
        try:
            return await Aerich.filter(app=cls.app).first()
        except OperationalError:
            pass

    @classmethod
    async def _get_db_version(cls, connection: BaseDBAsyncClient):
        if cls.dialect == "mysql":
            sql = "select version() as version"
            ret = await connection.execute_query(sql)
            cls._db_version = ret[1][0].get("version")

    @classmethod
    async def init(cls, config: dict, app: str, location: str):
        await Tortoise.init(config=config)
        last_version = await cls.get_last_version()
        cls.app = app
        cls.migrate_location = Path(location, app)
        if last_version:
            cls._last_version_content = last_version.content

        connection = get_app_connection(config, app)
        cls.dialect = connection.schema_generator.DIALECT
        if cls.dialect == "mysql":
            from aerich.ddl.mysql import MysqlDDL

            cls.ddl = MysqlDDL(connection)
        elif cls.dialect == "sqlite":
            from aerich.ddl.sqlite import SqliteDDL

            cls.ddl = SqliteDDL(connection)
        elif cls.dialect == "postgres":
            from aerich.ddl.postgres import PostgresDDL

            cls.ddl = PostgresDDL(connection)
        await cls._get_db_version(connection)

    @classmethod
    async def _get_last_version_num(cls):
        last_version = await cls.get_last_version()
        if not last_version:
            return None
        version = last_version.version
        return int(version.split("_", 1)[0])

    @classmethod
    async def generate_version(cls, name=None):
        now = datetime.now().strftime("%Y%m%d%H%M%S").replace("/", "")
        last_version_num = await cls._get_last_version_num()
        if last_version_num is None:
            return f"0_{now}_init.sql"
        version = f"{last_version_num + 1}_{now}_{name}.sql"
        if len(version) > MAX_VERSION_LENGTH:
            raise ValueError(f"Version name exceeds maximum length ({MAX_VERSION_LENGTH})")
        return version

    @classmethod
    async def _generate_diff_sql(cls, name):
        version = await cls.generate_version(name)
        # delete if same version exists
        for version_file in cls.get_all_version_files():
            if version_file.startswith(version.split("_")[0]):
                os.unlink(Path(cls.migrate_location, version_file))
        content = {
            "upgrade": cls.upgrade_operators,
            "downgrade": cls.downgrade_operators,
        }
        write_version_file(Path(cls.migrate_location, version), content)
        return version

    @classmethod
    async def migrate(cls, name) -> str:
        """
        diff old models and new models to generate diff content
        :param name:
        :return:
        """
        new_version_content = get_models_describe(cls.app)
        cls.diff_models(cls._last_version_content, new_version_content)
        cls._merge_operators()

        if not cls.upgrade_operators:
            return ""

        return await cls._generate_diff_sql(name)

    @classmethod
    def _add_operator(cls, operator: str, upgrade=True, fk_m2m=False):
        """
        add operator,differentiate fk because fk is order limit
        :param operator:
        :param upgrade:
        :param fk_m2m:
        :return:
        """
        if upgrade:
            if fk_m2m:
                cls._upgrade_fk_m2m_index_operators.append(operator)
            else:
                cls.upgrade_operators.append(operator)
        else:
            if fk_m2m:
                cls._downgrade_fk_m2m_index_operators.append(operator)
            else:
                cls.downgrade_operators.append(operator)

    # @classmethod
    # def diff_models(cls, old_models: Dict[str, dict], new_models: Dict[str, dict], upgrade=True):
    #     """
    #     diff models and add operators
    #     :param old_models:
    #     :param new_models:
    #     :param upgrade:
    #     :return:
    #     """
    #     _aerich = f"{cls.app}.{cls._aerich}"
    #     old_models.pop(_aerich, None)
    #     new_models.pop(_aerich, None)
    #
    #     for new_model_str, new_model_describe in new_models.items():
    #         model = cls._get_model(new_model_describe.get("name").split(".")[1])
    #         table_name = model._meta.db_table
    #         if new_model_str not in old_models.keys():
    #             if upgrade:
    #                 cls._add_operator(cls.add_model(model), upgrade)
    #             else:
    #                 # we can't find origin model when downgrade, so skip
    #                 pass
    #         else:
    #             old_model_describe = old_models.get(new_model_str)
    #             # rename table
    #             new_table: str = new_model_describe.get("table")
    #             old_table: str = old_model_describe.get("table")
    #             if new_table != old_table:
    #                 cls._add_operator(cls.rename_table(old_table, new_table), upgrade)
    #             old_unique_together = set(
    #                 map(lambda x: tuple(x), old_model_describe.get("unique_together"))
    #             )
    #             new_unique_together = set(
    #                 map(lambda x: tuple(x), new_model_describe.get("unique_together"))
    #             )
    #
    #             old_pk_field = old_model_describe.get("pk_field")
    #             new_pk_field = new_model_describe.get("pk_field")
    #             # pk field
    #             changes = diff(old_pk_field, new_pk_field)
    #             for action, option, change in changes:
    #                 # current only support rename pk
    #                 if action == "change" and option == "name":
    #                     cls._add_operator(cls._rename_field(table_name, *change), upgrade)
    #             # m2m fields
    #             old_m2m_fields = old_model_describe.get("m2m_fields")
    #             new_m2m_fields = new_model_describe.get("m2m_fields")
    #             for action, option, change in diff(old_m2m_fields, new_m2m_fields):
    #                 table = change[0][1].get("through")
    #                 if action == "add":
    #                     add = False
    #                     if upgrade and table not in cls._upgrade_m2m:
    #                         cls._upgrade_m2m.append(table)
    #                         add = True
    #                     elif not upgrade and table not in cls._downgrade_m2m:
    #                         cls._downgrade_m2m.append(table)
    #                         add = True
    #                     if add:
    #                         cls._add_operator(
    #                             cls.create_m2m(
    #                                 model.describe(),
    #                                 change[0][1],
    #                                 new_models.get(change[0][1].get("model_name")),
    #                             ),
    #                             upgrade,
    #                             fk_m2m=True,
    #                         )
    #                 elif action == "remove":
    #                     add = False
    #                     if upgrade and table not in cls._upgrade_m2m:
    #                         cls._upgrade_m2m.append(table)
    #                         add = True
    #                     elif not upgrade and table not in cls._downgrade_m2m:
    #                         cls._downgrade_m2m.append(table)
    #                         add = True
    #                     if add:
    #                         cls._add_operator(cls.drop_m2m(table), upgrade, fk_m2m=True)
    #             # add unique_together
    #             for index in new_unique_together.difference(old_unique_together):
    #                 cls._add_operator(
    #                     cls._add_index(table_name, index, True), upgrade,
    #                 )
    #             # remove unique_together
    #             for index in old_unique_together.difference(new_unique_together):
    #                 cls._add_operator(
    #                     cls._drop_index(table_name=table_name, fields_name=index, unique=True),
    #                     upgrade,
    #                 )
    #
    #             old_data_fields = old_model_describe.get("data_fields")
    #             new_data_fields = new_model_describe.get("data_fields")
    #
    #             old_data_fields_name = list(map(lambda x: x.get("name"), old_data_fields))
    #             new_data_fields_name = list(map(lambda x: x.get("name"), new_data_fields))
    #
    #             # add fields or rename fields
    #             for new_data_field_name in set(new_data_fields_name).difference(
    #                 set(old_data_fields_name)
    #             ):
    #                 new_data_field = next(
    #                     filter(lambda x: x.get("name") == new_data_field_name, new_data_fields)
    #                 )
    #                 is_rename = False
    #                 for old_data_field in old_data_fields:
    #                     changes = list(diff(old_data_field, new_data_field))
    #                     old_data_field_name = old_data_field.get("name")
    #                     if len(changes) == 2:
    #                         # rename field
    #                         if (
    #                             changes[0]
    #                             == ("change", "name", (old_data_field_name, new_data_field_name),)
    #                             and changes[1]
    #                             == (
    #                                 "change",
    #                                 "db_column",
    #                                 (
    #                                     old_data_field.get("db_column"),
    #                                     new_data_field.get("db_column"),
    #                                 ),
    #                             )
    #                             and old_data_field_name not in new_data_fields_name
    #                         ):
    #                             if upgrade:
    #                                 is_rename = click.prompt(
    #                                     f"Rename {old_data_field_name} to {new_data_field_name}?",
    #                                     default=True,
    #                                     type=bool,
    #                                     show_choices=True,
    #                                 )
    #                             else:
    #                                 is_rename = old_data_field_name in cls._rename_new
    #                             if is_rename:
    #                                 cls._rename_new.append(new_data_field_name)
    #                                 cls._rename_old.append(old_data_field_name)
    #                                 # only MySQL8+ has rename syntax
    #                                 if (
    #                                     cls.dialect == "mysql"
    #                                     and cls._db_version
    #                                     and cls._db_version.startswith("5.")
    #                                 ):
    #                                     cls._add_operator(
    #                                         cls._modify_field(table_name, new_data_field), upgrade,
    #                                     )
    #                                 else:
    #                                     cls._add_operator(
    #                                         cls._rename_field(table_name, *changes[1][2]), upgrade,
    #                                     )
    #                 if not is_rename:
    #                     cls._add_operator(
    #                         cls._add_field(table_name, new_data_field,), upgrade,
    #                     )
    #             # remove fields
    #             for old_data_field_name in set(old_data_fields_name).difference(
    #                 set(new_data_fields_name)
    #             ):
    #                 # don't remove field if is rename
    #                 if (upgrade and old_data_field_name in cls._rename_old) or (
    #                     not upgrade and old_data_field_name in cls._rename_new
    #                 ):
    #                     continue
    #                 cls._add_operator(
    #                     cls._remove_field(
    #                         table_name,
    #                         next(
    #                             filter(
    #                                 lambda x: x.get("name") == old_data_field_name, old_data_fields
    #                             )
    #                         ).get("db_column"),
    #                     ),
    #                     upgrade,
    #                 )
    #             old_fk_fields = old_model_describe.get("fk_fields")
    #             new_fk_fields = new_model_describe.get("fk_fields")
    #
    #             old_fk_fields_name = list(map(lambda x: x.get("name"), old_fk_fields))
    #             new_fk_fields_name = list(map(lambda x: x.get("name"), new_fk_fields))
    #
    #             # add fk
    #             for new_fk_field_name in set(new_fk_fields_name).difference(
    #                 set(old_fk_fields_name)
    #             ):
    #                 fk_field = next(
    #                     filter(lambda x: x.get("name") == new_fk_field_name, new_fk_fields)
    #                 )
    #                 cls._add_operator(
    #                     cls._add_fk(
    #                         table_name, fk_field, new_models.get(fk_field.get("python_type"))
    #                     ),
    #                     upgrade,
    #                     fk_m2m=True,
    #                 )
    #             # drop fk
    #             for old_fk_field_name in set(old_fk_fields_name).difference(
    #                 set(new_fk_fields_name)
    #             ):
    #                 old_fk_field = next(
    #                     filter(lambda x: x.get("name") == old_fk_field_name, old_fk_fields)
    #                 )
    #                 cls._add_operator(
    #                     cls._drop_fk(
    #                         table_name,
    #                         old_fk_field,
    #                         old_models.get(old_fk_field.get("python_type")),
    #                     ),
    #                     upgrade,
    #                     fk_m2m=True,
    #                 )
    #             # change fields
    #             for field_name in set(new_data_fields_name).intersection(set(old_data_fields_name)):
    #                 old_data_field = next(
    #                     filter(lambda x: x.get("name") == field_name, old_data_fields)
    #                 )
    #                 new_data_field = next(
    #                     filter(lambda x: x.get("name") == field_name, new_data_fields)
    #                 )
    #                 changes = diff(old_data_field, new_data_field)
    #                 for change in changes:
    #                     _, option, old_new = change
    #                     if option == "indexed":
    #                         # change index
    #                         unique = new_data_field.get("unique")
    #                         if old_new[0] is False and old_new[1] is True:
    #                             cls._add_operator(
    #                                 cls._add_index(table_name, (field_name,), unique), upgrade,
    #                             )
    #                         else:
    #                             cls._add_operator(
    #                                 cls._drop_index(table_name, (field_name,), unique), upgrade,
    #                             )
    #                     elif option == "db_field_types.":
    #                         # continue since repeated with others
    #                         continue
    #                     elif option == "default":
    #                         if not (
    #                             is_default_function(old_new[0]) or is_default_function(old_new[1])
    #                         ):
    #                             # change column default
    #                             cls._add_operator(
    #                                 cls._alter_default(table_name, new_data_field), upgrade
    #                             )
    #                     elif option == "unique":
    #                         # because indexed include it
    #                         pass
    #                     elif option == "nullable":
    #                         # change nullable
    #                         cls._add_operator(cls._alter_null(table_name, new_data_field), upgrade)
    #                     else:
    #                         # modify column
    #                         cls._add_operator(
    #                             cls._modify_field(table_name, new_data_field), upgrade,
    #                         )
    #
    #     for old_model in old_models:
    #         if old_model not in new_models.keys():
    #             cls._add_operator(cls.drop_model(old_models.get(old_model).get("table")), upgrade)

    @classmethod
    def diff_models(  # fixme:indexes联合索引好像没有变更和增加的操作。
        cls, old_models_describes: Dict[str, dict], new_models_describes: Dict[str, dict],
    ):
        """
        diff models and add operators
        """
        _aerich = f"{cls.app}.{cls._aerich}"
        old_models_describes.pop(_aerich, None)
        new_models_describes.pop(_aerich, None)
        for new_model_str, new_model_describe in new_models_describes.items():
            table_name = new_model_describe["table"]
            if new_model_str not in old_models_describes:
                cls._add_operator(cls.add_model(new_model_describe), True)
                cls._add_operator(cls.drop_model(table_name), False)
            else:
                old_model_describe = old_models_describes.get(new_model_str)
                # rename table
                new_table = new_model_describe.get("table")
                old_table = old_model_describe.get("table")
                if new_table != old_table:
                    cls._add_operator(cls.rename_table(old_table, new_table), True)
                    cls._add_operator(cls.rename_table(new_table, old_table), False)
                old_unique_together = set(
                    map(lambda x: tuple(x), old_model_describe.get("unique_together"))
                )
                new_unique_together = set(
                    map(lambda x: tuple(x), new_model_describe.get("unique_together"))
                )

                old_pk_field = old_model_describe.get("pk_field")
                new_pk_field = new_model_describe.get("pk_field")
                # pk field
                changes = diff(old_pk_field, new_pk_field)
                for action, option, change in changes:
                    # current only support rename pk
                    if action == "change" and option == "name":
                        cls._add_operator(cls._rename_field(table_name, *change), True)
                        change.reverse()
                        cls._add_operator(cls._rename_field(table_name, *change), False)

                # m2m fields
                # fixme：需要详细测试
                old_m2m_fields = old_model_describe.get("m2m_fields")
                new_m2m_fields = new_model_describe.get("m2m_fields")
                for action, option, change in diff(old_m2m_fields, new_m2m_fields):
                    table = change[0][1].get("through")
                    if table not in cls._upgrade_m2m:
                        cls._upgrade_m2m.append(table)
                    if action == "add":
                        cls._add_operator(
                            cls.create_m2m(
                                new_model_describe,
                                change[0][1],
                                new_models_describes.get(change[0][1].get("model_name")),
                            ),
                            True,
                            fk_m2m=True,
                        )
                        cls._add_operator(
                            cls.drop_m2m(table), False, fk_m2m=True,
                        )
                    elif action == "remove":
                        cls._add_operator(cls.drop_m2m(table), True, fk_m2m=True)
                        cls._add_operator(
                            cls.create_m2m(
                                old_model_describe,
                                change[0][1],
                                old_models_describes.get(change[0][1].get("model_name")),
                            ),
                            False,
                            fk_m2m=True,
                        )
                # add unique_together
                for index in new_unique_together.difference(old_unique_together):
                    cls._add_operator(
                        cls._add_index(table_name, index, True), True,
                    )
                    cls._add_operator(
                        cls._drop_index(table_name, index, True), False,
                    )
                # remove unique_together
                for index in old_unique_together.difference(new_unique_together):
                    cls._add_operator(
                        cls._drop_index(table_name, index, True), True,
                    )
                    cls._add_operator(
                        cls._add_index(table_name, index, True), False,
                    )

                old_data_fields = old_model_describe.get("data_fields")
                new_data_fields = new_model_describe.get("data_fields")

                old_data_fields_name = list(map(lambda x: x.get("name"), old_data_fields))
                new_data_fields_name = list(map(lambda x: x.get("name"), new_data_fields))

                # add fields or rename fields
                for new_data_field_name in set(new_data_fields_name).difference(
                    set(old_data_fields_name)
                ):
                    new_data_field = next(
                        filter(lambda x: x.get("name") == new_data_field_name, new_data_fields)
                    )
                    is_rename = False
                    for old_data_field in old_data_fields:
                        changes = list(diff(old_data_field, new_data_field))
                        old_data_field_name = old_data_field.get("name")
                        if len(changes) == 2:
                            # rename field
                            if (
                                changes[0]
                                == ("change", "name", (old_data_field_name, new_data_field_name),)
                                and changes[1]
                                == (
                                    "change",
                                    "db_column",
                                    (
                                        old_data_field.get("db_column"),
                                        new_data_field.get("db_column"),
                                    ),
                                )
                                and old_data_field_name not in new_data_fields_name
                            ):
                                is_rename = click.prompt(
                                    f"Rename {old_data_field_name} to {new_data_field_name}?",
                                    default=True,
                                    type=bool,
                                    show_choices=True,
                                )
                                if is_rename:
                                    cls._rename_new.append(new_data_field_name)
                                    cls._rename_old.append(old_data_field_name)
                                    # only MySQL8+ has rename syntax
                                    if (
                                        cls.dialect == "mysql"
                                        and cls._db_version
                                        and cls._db_version.startswith("5.")
                                    ):
                                        cls._add_operator(
                                            cls._modify_field(table_name, new_data_field), True,
                                        )
                                        cls._add_operator(
                                            cls._modify_field(table_name, old_data_field), False,
                                        )
                                    else:
                                        names = changes[1][2]
                                        cls._add_operator(
                                            cls._rename_field(table_name, *names), True,
                                        )
                                        names.reverse()
                                        cls._add_operator(
                                            cls._rename_field(table_name, *names), False,
                                        )
                    if not is_rename:
                        cls._add_operator(
                            cls._add_field(table_name, new_data_field,), True,
                        )

                        cls._add_operator(
                            cls._remove_field(table_name, new_data_field["db_column"],), False,
                        )
                # remove fields
                for old_data_field_name in set(old_data_fields_name).difference(
                    set(new_data_fields_name)
                ):
                    # don't remove field if is rename
                    if old_data_field_name in cls._rename_old:
                        continue
                    cls._add_operator(
                        cls._remove_field(
                            table_name,
                            next(
                                filter(
                                    lambda x: x.get("name") == old_data_field_name, old_data_fields
                                )
                            ).get("db_column"),
                        ),
                        True,
                    )
                    cls._add_operator(
                        cls._add_field(
                            table_name,
                            next(
                                filter(
                                    lambda x: x.get("name") == old_data_field_name, old_data_fields
                                )
                            ),
                        ),
                        False,
                    )
                old_fk_fields = old_model_describe.get("fk_fields")
                new_fk_fields = new_model_describe.get("fk_fields")

                old_fk_fields_name = list(map(lambda x: x.get("name"), old_fk_fields))
                new_fk_fields_name = list(map(lambda x: x.get("name"), new_fk_fields))

                # add fk
                for new_fk_field_name in set(new_fk_fields_name).difference(
                    set(old_fk_fields_name)
                ):
                    fk_field = next(
                        filter(lambda x: x.get("name") == new_fk_field_name, new_fk_fields)
                    )
                    cls._add_operator(
                        cls._add_fk(
                            table_name,
                            fk_field,
                            new_models_describes.get(fk_field.get("python_type")),
                        ),
                        True,
                        fk_m2m=True,
                    )
                    cls._add_operator(
                        cls._drop_fk(
                            table_name,
                            fk_field,
                            new_models_describes.get(fk_field.get("python_type")),
                        ),
                        False,
                        fk_m2m=True,
                    )
                # drop fk
                for old_fk_field_name in set(old_fk_fields_name).difference(
                    set(new_fk_fields_name)
                ):
                    old_fk_field = next(
                        filter(lambda x: x.get("name") == old_fk_field_name, old_fk_fields)
                    )
                    cls._add_operator(
                        cls._drop_fk(
                            table_name,
                            old_fk_field,
                            old_models_describes.get(old_fk_field.get("python_type")),
                        ),
                        True,
                        fk_m2m=True,
                    )
                    cls._add_operator(
                        cls._add_fk(
                            table_name,
                            old_fk_field,
                            old_models_describes.get(old_fk_field.get("python_type")),
                        ),
                        False,
                        fk_m2m=True,
                    )
                # change fields
                for field_name in set(new_data_fields_name).intersection(set(old_data_fields_name)):
                    old_data_field = next(
                        filter(lambda x: x.get("name") == field_name, old_data_fields)
                    )
                    new_data_field = next(
                        filter(lambda x: x.get("name") == field_name, new_data_fields)
                    )
                    changes = diff(old_data_field, new_data_field)
                    for change in changes:
                        _, option, old_new = change
                        if option == "indexed":
                            # change index
                            unique = new_data_field.get("unique")
                            if old_new[0] is False and old_new[1] is True:
                                cls._add_operator(
                                    cls._add_index(table_name, (field_name,), unique), True,
                                )
                                cls._add_operator(
                                    cls._drop_index(table_name, (field_name,), unique), False,
                                )
                            else:
                                cls._add_operator(
                                    cls._drop_index(table_name, (field_name,), unique), True,
                                )
                                cls._add_operator(
                                    cls._add_index(table_name, (field_name,), unique), False,
                                )
                        elif option == "db_field_types.":
                            # continue since repeated with others
                            continue
                        elif option == "default":
                            if not (
                                is_default_function(old_new[0]) or is_default_function(old_new[1])
                            ):
                                # change column default
                                cls._add_operator(
                                    cls._alter_default(table_name, new_data_field), True
                                )
                                cls._add_operator(
                                    cls._alter_default(table_name, old_data_field), False
                                )
                        elif option == "unique":
                            # because indexed include it
                            pass
                        elif option == "nullable":
                            # change nullable
                            cls._add_operator(cls._alter_null(table_name, new_data_field), True)
                            cls._add_operator(cls._alter_null(table_name, old_data_field), False)

                        else:
                            if (
                                cls._modified_fields.get(table_name)
                                and new_data_field["db_column"] in cls._modified_fields[table_name]
                            ):
                                continue
                            # modify column
                            cls._add_operator(
                                cls._modify_field(table_name, new_data_field), True,
                            )
                            cls._add_operator(
                                cls._modify_field(table_name, old_data_field), False,
                            )
                            if cls._modified_fields.get(table_name):
                                cls._modified_fields[table_name].add(new_data_field["db_column"])
                            else:
                                cls._modified_fields[table_name] = {
                                    new_data_field["db_column"],
                                }

        for old_model in old_models_describes:
            if old_model not in new_models_describes.keys():
                cls._add_operator(
                    cls.drop_model(old_models_describes.get(old_model).get("table")), True
                )
                cls._add_operator(cls.add_model(old_models_describes.get(old_model)), False)

    @classmethod
    def rename_table(cls, old_table_name: str, new_table_name: str):
        return cls.ddl.rename_table(old_table_name, new_table_name)

    @classmethod
    def add_model(cls, table_describe):
        return cls.ddl.create_table(table_describe)

    @classmethod
    def drop_model(cls, table_name: str):
        return cls.ddl.drop_table(table_name)

    @classmethod
    def create_m2m(cls, table_describe: dict, field_describe: dict, reference_table_describe: dict):
        return cls.ddl.create_m2m(table_describe, field_describe, reference_table_describe)

    @classmethod
    def drop_m2m(cls, table_name: str):
        return cls.ddl.drop_m2m(table_name)

    @classmethod
    def _resolve_fk_fields_name(cls, fk_fields: List[dict], fields_name: Tuple[str]):
        ret = []
        for field_name in fields_name:
            if field_name in fk_fields:
                ret.append(field_name + "_id")
            else:
                ret.append(field_name)
        return ret

    @classmethod
    def _drop_index(cls, table_name: str, fields_name: Tuple[str, ...], unique=False):
        return cls.ddl.drop_index(table_name, fields_name, unique)

    @classmethod
    def _add_index(cls, table_name: str, fields_name: Tuple[str, ...], unique=False):
        return cls.ddl.add_index(table_name, fields_name, unique)

    @classmethod
    def _add_field(cls, table_name: str, field_describe: dict, is_pk: bool = False):
        return cls.ddl.add_column(table_name, field_describe, is_pk)

    @classmethod
    def _alter_default(cls, table_name: str, field_describe: dict):
        return cls.ddl.alter_column_default(table_name, field_describe)

    @classmethod
    def _alter_null(cls, table_name: str, field_describe: dict):
        return cls.ddl.alter_column_null(table_name, field_describe)

    @classmethod
    def _set_comment(cls, table_name: str, field_describe: dict):
        return cls.ddl.set_comment(table_name, field_describe)

    @classmethod
    def _modify_field(cls, table_name: str, field_describe: dict):
        return cls.ddl.modify_column(table_name, field_describe)

    @classmethod
    def _drop_fk(cls, table_name: str, field_describe: dict, reference_table_describe: dict):
        return cls.ddl.drop_fk(table_name, field_describe, reference_table_describe)

    @classmethod
    def _remove_field(cls, table_name: str, column_name: str):
        return cls.ddl.drop_column(table_name, column_name)

    @classmethod
    def _rename_field(cls, table_name: str, old_field_name: str, new_field_name: str):
        return cls.ddl.rename_column(table_name, old_field_name, new_field_name)

    @classmethod
    def _change_field(cls, table_name: str, old_field_describe: dict, new_field_describe: dict):
        db_field_types = new_field_describe.get("db_field_types")
        return cls.ddl.change_column(
            table_name,
            old_field_describe.get("db_column"),
            new_field_describe.get("db_column"),
            db_field_types.get(cls.dialect) or db_field_types.get(""),
        )

    @classmethod
    def _add_fk(cls, table_name: str, field_describe: dict, reference_table_describe: dict):
        """
        add fk
        """
        return cls.ddl.add_fk(table_name, field_describe, reference_table_describe)

    @classmethod
    def _merge_operators(cls):
        """
        fk/m2m/index must be last when add,first when drop
        :return:
        """
        for _upgrade_fk_m2m_operator in cls._upgrade_fk_m2m_index_operators:
            if "ADD" in _upgrade_fk_m2m_operator or "CREATE" in _upgrade_fk_m2m_operator:
                cls.upgrade_operators.append(_upgrade_fk_m2m_operator)
            else:
                cls.upgrade_operators.insert(0, _upgrade_fk_m2m_operator)

        for _downgrade_fk_m2m_operator in cls._downgrade_fk_m2m_index_operators:
            if "ADD" in _downgrade_fk_m2m_operator or "CREATE" in _downgrade_fk_m2m_operator:
                cls.downgrade_operators.append(_downgrade_fk_m2m_operator)
            else:
                cls.downgrade_operators.insert(0, _downgrade_fk_m2m_operator)
