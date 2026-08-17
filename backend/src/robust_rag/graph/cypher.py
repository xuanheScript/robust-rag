"""Lexical and structural guard for model-generated read-only Cypher."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CypherValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TokenKind(StrEnum):
    WORD = "word"
    STRING = "string"
    NUMBER = "number"
    PARAMETER = "parameter"
    SYMBOL = "symbol"


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    value: str
    offset: int

    @property
    def upper(self) -> str:
        return self.value.upper()


@dataclass(frozen=True)
class ValidatedCypher:
    query: str
    limit: int
    max_depth: int
    labels: frozenset[str]
    relationship_types: frozenset[str]
    properties: frozenset[str]


PROHIBITED = frozenset(
    {
        "CREATE",
        "MERGE",
        "DELETE",
        "DETACH",
        "SET",
        "REMOVE",
        "DROP",
        "ALTER",
        "GRANT",
        "DENY",
        "REVOKE",
        "LOAD",
        "CSV",
        "CALL",
        "YIELD",
        "UNION",
        "SHOW",
        "USE",
        "FOREACH",
        "TRANSACTION",
        "TERMINATE",
        "RENAME",
        "START",
    }
)
CLAUSES = frozenset({"MATCH", "OPTIONAL", "WHERE", "WITH", "RETURN", "ORDER", "SKIP", "LIMIT"})
ALLOWED_LABELS = frozenset({"Entity", "GraphFact", "RetrievalNode", "Document", "DocumentVersion"})
ALLOWED_RELATIONSHIPS = frozenset(
    {"SUBJECT", "OBJECT", "SUPPORTED_BY", "HAS_VERSION", "CONTAINS", "NEXT"}
)
ALLOWED_PROPERTIES = frozenset(
    {
        "id",
        "entity_id",
        "fact_id",
        "entity_type",
        "primary_name",
        "normalized_name",
        "aliases",
        "schema_version",
        "predicate",
        "review_status",
        "origin",
        "confidence",
        "active",
        "document_id",
        "version_id",
        "node_id",
        "source_node_id",
        "title",
        "heading_path",
        "source_locators",
        "content",
        "name",
    }
)
ALLOWED_FUNCTIONS = frozenset({"count", "collect", "coalesce", "tolower", "size", "type"})


def tokenize_cypher(query: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    size = len(query)
    while index < size:
        character = query[index]
        if character.isspace():
            index += 1
            continue
        if query.startswith("//", index):
            end = query.find("\n", index + 2)
            index = size if end < 0 else end + 1
            continue
        if query.startswith("/*", index):
            end = query.find("*/", index + 2)
            if end < 0:
                raise CypherValidationError("CYPHER_UNTERMINATED_COMMENT", "Unterminated comment")
            index = end + 2
            continue
        if character in {"'", '"'}:
            quote = character
            start = index
            index += 1
            string_parts: list[str] = []
            while index < size:
                if query[index] == "\\" and index + 1 < size:
                    string_parts.extend((query[index], query[index + 1]))
                    index += 2
                elif query[index] == quote:
                    index += 1
                    break
                else:
                    string_parts.append(query[index])
                    index += 1
            else:
                raise CypherValidationError("CYPHER_UNTERMINATED_STRING", "Unterminated string")
            tokens.append(Token(TokenKind.STRING, "".join(string_parts), start))
            continue
        if character == "`":
            start = index
            index += 1
            identifier_parts: list[str] = []
            while index < size:
                if query.startswith("``", index):
                    identifier_parts.append("`")
                    index += 2
                elif query[index] == "`":
                    index += 1
                    break
                else:
                    identifier_parts.append(query[index])
                    index += 1
            else:
                raise CypherValidationError(
                    "CYPHER_UNTERMINATED_IDENTIFIER", "Unterminated identifier"
                )
            tokens.append(Token(TokenKind.WORD, "".join(identifier_parts), start))
            continue
        if character == "$":
            start = index
            index += 1
            while index < size and (query[index].isalnum() or query[index] == "_"):
                index += 1
            if index == start + 1:
                raise CypherValidationError("CYPHER_INVALID_PARAMETER", "Invalid parameter")
            tokens.append(Token(TokenKind.PARAMETER, query[start + 1 : index], start))
            continue
        if character.isalpha() or character == "_":
            start = index
            index += 1
            while index < size and (query[index].isalnum() or query[index] == "_"):
                index += 1
            tokens.append(Token(TokenKind.WORD, query[start:index], start))
            continue
        if character.isdigit():
            start = index
            index += 1
            while index < size and query[index].isdigit():
                index += 1
            tokens.append(Token(TokenKind.NUMBER, query[start:index], start))
            continue
        matched = next(
            (
                symbol
                for symbol in ("<->", "<-", "->", "<=", ">=", "<>", "!=", "..")
                if query.startswith(symbol, index)
            ),
            None,
        )
        if matched:
            tokens.append(Token(TokenKind.SYMBOL, matched, index))
            index += len(matched)
        elif character in "()[]{}:.,;*+-/=<>|":
            tokens.append(Token(TokenKind.SYMBOL, character, index))
            index += 1
        else:
            raise CypherValidationError(
                "CYPHER_INVALID_CHARACTER", f"Unsupported character at offset {index}"
            )
    return tokens


class CypherValidator:
    def __init__(
        self,
        *,
        max_depth: int = 3,
        max_rows: int = 50,
        allowed_labels: frozenset[str] = ALLOWED_LABELS,
        allowed_relationships: frozenset[str] = ALLOWED_RELATIONSHIPS,
        allowed_properties: frozenset[str] = ALLOWED_PROPERTIES,
    ) -> None:
        self.max_depth = max_depth
        self.max_rows = max_rows
        self.allowed_labels = allowed_labels
        self.allowed_relationships = allowed_relationships
        self.allowed_properties = allowed_properties

    def __call__(self, query: str) -> str:
        return self.validate(query).query

    def validate(self, query: str) -> ValidatedCypher:
        tokens = tokenize_cypher(query.strip())
        if not tokens:
            raise CypherValidationError("CYPHER_EMPTY", "Cypher query is empty")
        if any(token.value == ";" for token in tokens):
            raise CypherValidationError("CYPHER_MULTIPLE_STATEMENTS", "Semicolons are not allowed")
        words = [token.upper for token in tokens if token.kind is TokenKind.WORD]
        prohibited = next((word for word in words if word in PROHIBITED), None)
        if prohibited:
            raise CypherValidationError(
                "CYPHER_WRITE_OR_UNSAFE", f"Clause {prohibited} is not allowed"
            )
        if "MATCH" not in words or "RETURN" not in words:
            raise CypherValidationError("CYPHER_REQUIRED_CLAUSE", "MATCH and RETURN are required")
        unknown_clause = next(
            (word for word in words if word in {"UNWIND", "EXISTS", "SUBQUERY"}), None
        )
        if unknown_clause:
            raise CypherValidationError(
                "CYPHER_UNSUPPORTED_CLAUSE", f"Clause {unknown_clause} is not allowed"
            )

        labels: set[str] = set()
        relationships: set[str] = set()
        properties: set[str] = set()
        depths: list[int] = []
        bracket_depth = 0
        curly_depth = 0
        for index, token in enumerate(tokens):
            if token.value == "[":
                bracket_depth += 1
            elif token.value == "]":
                bracket_depth = max(0, bracket_depth - 1)
            elif token.value == "{":
                curly_depth += 1
            elif token.value == "}":
                curly_depth = max(0, curly_depth - 1)
            elif token.value == ":" and index + 1 < len(tokens):
                name = tokens[index + 1]
                if name.kind is not TokenKind.WORD:
                    raise CypherValidationError(
                        "CYPHER_INVALID_SCHEMA_TOKEN", "Invalid label or relationship"
                    )
                if curly_depth:
                    if index > 0 and tokens[index - 1].kind is TokenKind.WORD:
                        properties.add(tokens[index - 1].value)
                elif bracket_depth:
                    relationships.add(name.value)
                else:
                    labels.add(name.value)
            elif (
                token.value == "|"
                and bracket_depth
                and index + 1 < len(tokens)
                and tokens[index + 1].kind is TokenKind.WORD
            ):
                relationships.add(tokens[index + 1].value)
            elif token.value == "." and index + 1 < len(tokens):
                name = tokens[index + 1]
                if name.kind is TokenKind.WORD:
                    properties.add(name.value)
            elif token.value == "*":
                depth = self._bounded_depth(tokens, index)
                depths.append(depth)

        invalid_label = next((value for value in labels if value not in self.allowed_labels), None)
        if invalid_label:
            raise CypherValidationError("CYPHER_SCHEMA_LABEL", f"Unknown label: {invalid_label}")
        invalid_rel = next(
            (value for value in relationships if value not in self.allowed_relationships), None
        )
        if invalid_rel:
            raise CypherValidationError(
                "CYPHER_SCHEMA_RELATIONSHIP", f"Unknown relationship: {invalid_rel}"
            )
        for index, token in enumerate(tokens[:-1]):
            if (
                token.kind is TokenKind.WORD
                and tokens[index + 1].value == "("
                and token.upper not in CLAUSES
                and token.value.casefold() not in ALLOWED_FUNCTIONS
            ):
                raise CypherValidationError(
                    "CYPHER_FUNCTION", f"Function {token.value} is not allowed"
                )

        invalid_property = next(
            (value for value in properties if value not in self.allowed_properties), None
        )
        if invalid_property:
            raise CypherValidationError(
                "CYPHER_SCHEMA_PROPERTY", f"Unknown property: {invalid_property}"
            )
        if not relationships and "WHERE" not in words:
            raise CypherValidationError(
                "CYPHER_UNCONSTRAINED_SCAN",
                "A single-node graph scan requires a bounded WHERE predicate",
            )

        limit_indices = [index for index, token in enumerate(tokens) if token.upper == "LIMIT"]
        if len(limit_indices) > 1:
            raise CypherValidationError("CYPHER_LIMIT", "Only one LIMIT is allowed")
        if limit_indices:
            index = limit_indices[0]
            if index + 1 >= len(tokens) or tokens[index + 1].kind is not TokenKind.NUMBER:
                raise CypherValidationError("CYPHER_LIMIT", "LIMIT must be a literal integer")
            requested = int(tokens[index + 1].value)
            if requested < 1:
                raise CypherValidationError("CYPHER_LIMIT", "LIMIT must be positive")
            limit = min(requested, self.max_rows)
            if requested != limit:
                start = tokens[index + 1].offset
                end = start + len(tokens[index + 1].value)
                query = f"{query[:start]}{limit}{query[end:]}"
        else:
            limit = self.max_rows
            query = f"{query.rstrip()} LIMIT {limit}"
        return ValidatedCypher(
            query=query,
            limit=limit,
            max_depth=max(depths, default=1),
            labels=frozenset(labels),
            relationship_types=frozenset(relationships),
            properties=frozenset(properties),
        )

    def _bounded_depth(self, tokens: list[Token], index: int) -> int:
        if index + 1 >= len(tokens) or tokens[index + 1].kind is not TokenKind.NUMBER:
            raise CypherValidationError(
                "CYPHER_UNBOUNDED_PATH", "Variable paths require an explicit range"
            )
        lower = int(tokens[index + 1].value)
        upper = lower
        if index + 2 < len(tokens) and tokens[index + 2].value == "..":
            if index + 3 >= len(tokens) or tokens[index + 3].kind is not TokenKind.NUMBER:
                raise CypherValidationError(
                    "CYPHER_UNBOUNDED_PATH", "Variable paths require an upper bound"
                )
            upper = int(tokens[index + 3].value)
        if lower < 1 or upper < lower or upper > self.max_depth:
            raise CypherValidationError(
                "CYPHER_PATH_DEPTH", f"Path depth must be between 1 and {self.max_depth}"
            )
        return upper
