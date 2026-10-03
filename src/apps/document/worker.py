from celery import shared_task
from django.db import transaction

import logging
import os
import tempfile
import requests

from .models import Document, DocumentChunk


logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    ignore_result=True,
    max_retries=3,
    default_retry_delay=60,
)
def DocumentProcessWorker(self, instance_id):
    """
    Download and process a document asynchronously.

    Pipeline:
    1. Fetch document from database.
    2. Mark it as PROCESSING.
    3. Download the file.
    4. Extract text.
    5. Split text into chunks.
    6. Generate embeddings.
    7. Save chunks.
    8. Mark document as READY.

    Heavy RAG modules are imported lazily to avoid loading
    PaddleOCR during Django/Celery startup.
    """

    doc = Document.objects.filter(id=instance_id).first()

    if not doc:
        logger.warning(
            "Document %s not found. Skipping task.",
            instance_id,
        )
        return

    try:
        # Import heavy modules only when processing begins.
        from .RAG import chunking, embedding, extract_text

        # Mark document as processing.
        doc.status = Document.Status.PROCESSING
        doc.save(update_fields=["status"])

        if not doc.file:
            raise ValueError(
                f"Document {instance_id} has no uploaded file."
            )

        # Download source file.
        response = requests.get(
            doc.file.url,
            timeout=(15, 120),
        )
        response.raise_for_status()

        # Determine file extension.
        file_format = os.path.splitext(
            doc.file.url.split("?")[0]
        )[1].lower()

        if not file_format:
            raise ValueError(
                "Could not determine the document file format."
            )

        # Save the downloaded file temporarily.
        with tempfile.NamedTemporaryFile(
            suffix=file_format,
        ) as temp_file:
            temp_file.write(response.content)
            temp_file.flush()

            # Extract text from the source file.
            documents = extract_text.extract_document(
                file_format=file_format,
                document_path=temp_file.name,
            )

        if not documents:
            raise ValueError(
                "No text could be extracted from the document."
            )

        # Split extracted content into chunks.
        chunks = chunking.chunking(documents)

        if not chunks:
            raise ValueError(
                "No chunks were generated from the document."
            )

        # Generate embeddings and save chunks.
        # Use a transaction so partial chunk writes are rolled back.
        with transaction.atomic():
            # Clear old chunks if this task is being retried.
            DocumentChunk.objects.filter(
                document=doc
            ).delete()

            for index, chunk in enumerate(chunks, start=1):
                text = chunk["text"]

                if not text or not text.strip():
                    continue

                embedding_vector = embedding.generate_embedding(
                    text
                )

                DocumentChunk.objects.create(
                    document=doc,
                    chunk_index=index,
                    text=text,
                    embedding=embedding_vector,
                    metadata={
                        "start": chunk["metadata_start"],
                        "end": chunk["metadata_end"],
                    },
                    token_count=len(text.split()),
                )

            # Mark as ready only after all chunks are saved.
            doc.status = Document.Status.READY
            doc.save(update_fields=["status"])

        logger.info(
            "Successfully processed document %s with %s chunks.",
            instance_id,
            len(chunks),
        )

    except Exception as exc:
        logger.exception(
            "Failed to process document %s.",
            instance_id,
        )

        # Mark failure before retrying.
        doc.status = Document.Status.FAILED
        doc.save(update_fields=["status"])

        # Retry transient failures, but do not retry indefinitely.
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)

        raise
