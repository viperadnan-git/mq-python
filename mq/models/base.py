from typing import Any, ClassVar, Self

from pydantic import BaseModel
from pydantic_extra_types.mongo_object_id import MongoObjectId
from pymongo.collection import Collection


class BaseMongoModel(BaseModel):
    _client: ClassVar[Collection]
    _id_field: ClassVar[str] = "id"

    @property
    def _id(self) -> Any:
        return getattr(self, self._id_field)

    def save(self) -> Self:
        self._client.update_one(
            {"_id": self._id},
            {"$set": self.model_dump(exclude={"id"})},
            upsert=True,
        )
        return self

    def delete(self) -> None:
        self._client.delete_one({"_id": self._id})

    @classmethod
    def _set_client(cls, client: Collection) -> Self:
        cls._client = client
        return cls

    @classmethod
    def create(cls, **kwargs) -> Self:
        return cls(**kwargs).save()

    @classmethod
    def find_one(cls, id: str) -> Self:
        response = cls._client.find_one(cls._serialize_id(id))
        if response:
            return cls(**response)
        return None

    @classmethod
    def _serialize_id(cls, id: Any) -> Any:
        try:
            return MongoObjectId.validate(id)
        except ValueError:
            return id

    @classmethod
    def delete_one(cls, id: Any) -> None:
        cls._client.delete_one({"_id": cls._serialize_id(id)})

    @classmethod
    def update_one(cls, id: Any, **kwargs) -> None:
        cls._client.update_one({"_id": cls._serialize_id(id)}, {"$set": kwargs})
