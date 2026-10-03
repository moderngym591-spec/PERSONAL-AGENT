from rest_framework import serializers

from .models import Document, DocumentChunk
from .worker import DocumentProcessWorker


class DocumentSerializer(serializers.ModelSerializer):
    email = serializers.CharField(source="user.email", read_only=True)

    class Meta:
        model = Document
        fields = [
            "id",
            "title",
            "email",
            "file",
            "file_size",
            "mime_type",
            "uploaded",
            "status",
        ]

        extra_kwargs = {
            "user": {"read_only": True},
            "file_size": {"read_only": True},
            "mime_type": {"read_only": True},
            "status": {"read_only": True},
        }

    def validate(self, attrs):
        if not attrs.get("title"):
            raise serializers.ValidationError(
                {"title": "Please provide a title"}
            )

        if not attrs.get("file"):
            raise serializers.ValidationError(
                {"file": "File is required"}
            )

        return attrs

    def create(self, validated_data):
        request = self.context.get("request")

        if request and request.user.is_authenticated:
            validated_data["user"] = request.user

        uploaded_file = validated_data["file"]
        validated_data["file_size"] = uploaded_file.size
        validated_data["mime_type"] = getattr(
            uploaded_file, "content_type", None
        ) or ""

        document = Document.objects.create(**validated_data)

        DocumentProcessWorker.delay(document.id)

        return document


class SimilarChunksSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentChunk
        fields = ["document", "text", "metadata", "created_at"]


class SourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentChunk
        fields = ["metadata"]
