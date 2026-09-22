"""Synthetic Google Docs structure; no real document contents or credentials."""


def nested_document():
    return {
        "documentId": "fixture_nested", "title": "Nested structural response",
        "revisionId": "revision-1", "body": {"content": [
            {"startIndex": index * 24 + 1, "endIndex": index * 24 + 25,
             "paragraph": {
                 "elements": [{"startIndex": index * 24 + 1, "endIndex": index * 24 + 25,
                               "textRun": {"content": "Полный текст строки 🧪\n", "textStyle": {
                                   "bold": False, "weightedFontFamily": {"fontFamily": "Arial"},
                                   "fontSize": {"magnitude": 11, "unit": "PT"}}}}],
                 "paragraphStyle": {"namedStyleType": "NORMAL_TEXT", "spaceAbove": {"magnitude": 0, "unit": "PT"}}}}
            for index in range(350)
        ]},
    }
