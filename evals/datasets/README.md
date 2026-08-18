# Golden datasets

`enterprise-golden-v1.json` follows `golden-dataset/1.0` and contains the first 50
seed questions. Its UUIDs identify the versioned seed corpus described by each
sample's `metadata.seed_document_key`. During the real-corpus acceptance import,
map those identities to the imported `Document.id` values without changing sample
IDs; any ground-truth change requires a new dataset version.

Default test and retrieval-only evaluation are deterministic and make no paid calls.
Generation and Ragas scoring require explicit request flags and configured model
adapters.
