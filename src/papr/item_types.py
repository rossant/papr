"""Convert source publication types to CSL without assuming journal publication."""

ZOTERO_TYPES = {
    "journalArticle": "article-journal",
    "magazineArticle": "article-magazine",
    "newspaperArticle": "article-newspaper",
    "conferencePaper": "paper-conference",
    "book": "book",
    "bookSection": "chapter",
    "thesis": "thesis",
    "report": "report",
    "preprint": "article",
    "manuscript": "manuscript",
    "webpage": "webpage",
    "blogPost": "post-weblog",
    "dataset": "dataset",
    "patent": "patent",
    "presentation": "speech",
    "document": "document",
    "letter": "personal_communication",
    "email": "personal_communication",
    "interview": "interview",
    "film": "motion_picture",
    "artwork": "graphic",
    "audioRecording": "song",
    "videoRecording": "motion_picture",
    "computerProgram": "software",
    "map": "map",
    "encyclopediaArticle": "entry-encyclopedia",
    "dictionaryEntry": "entry-dictionary",
    "forumPost": "post",
    "hearing": "hearing",
    "statute": "legislation",
    "case": "legal_case",
    "bill": "bill",
    "podcast": "broadcast",
    "radioBroadcast": "broadcast",
    "tvBroadcast": "broadcast",
    "instantMessage": "personal_communication",
}

SOURCE_TYPES = {
    "journal-article": "article-journal",
    "proceedings-article": "paper-conference",
    "book-chapter": "chapter",
    "book-part": "chapter",
    "book-section": "chapter",
    "monograph": "book",
    "edited-book": "book",
    "reference-book": "book",
    "posted-content": "article",
    "preprint": "article",
    "dissertation": "thesis",
    "article": "article-journal",
    "book": "book",
    "conference-paper": "paper-conference",
    "other": "document",
}


def from_zotero(value: str | None) -> str:
    return ZOTERO_TYPES.get(value or "", "document")


def from_source(value: str | None) -> str:
    return SOURCE_TYPES.get(value or "", "document")
