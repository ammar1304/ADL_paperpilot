# Writing E1 queries

Each row of `worksheet.csv` asks for one search query that a researcher could plausibly type and that the paper in `relevant_papers` answers. The `context` column shows material from that paper.

## The three types

| Type | Write a query that... | Example |
| --- | --- | --- |
| `paraphrase` | Describes the paper's main idea in your own words, as if looking for it | "rewrite the search query after each retrieval step to answer multi-hop questions" |
| `section` | Asks about something specific explained in one section (method detail, setup, analysis), using the section list | "how many passages are retrieved per step in iterative query rewriting" |
| `table` | Asks for a result reported in one of the listed tables | "exact match of query rewriting on HotpotQA compared to single-step retrieval" |

## Rules

1. **Your own words.** Never copy more than 4 consecutive words from the title or abstract; a copied query only tests string matching.
2. **No paper names, author names or arXiv ids** in the query.
3. **One information need per query,** 8 to 25 words.
4. **Check the paper if unsure.** For `section` and `table` rows, open the paper on arXiv to make sure it really answers your query.
5. **Add other relevant papers.** If you know another corpus paper also answers the query, add its id to `relevant_papers`, separated by `;`.
6. **Skip rows you cannot do well.** Leave `query` empty; empty rows are ignored.
7. **Don't edit after evaluating.** Once a query has been evaluated, do not change it.

Split the rows between two or three teammates and have one other person review them before the first run.
