"""validate_read_query is the only barrier between the retry-capable read transport and a CRM write."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'brain/src'))
from gtm.twenty import validate_read_query


def rejected(document):
    try:
        validate_read_query(document)
    except ValueError as error:
        return str(error) == 'twenty_read_query_required'
    return False


class ReadGuard(unittest.TestCase):
    def test_plain_reads_pass(self):
        for doc in ('query($e:String!){people(filter:{emails:{primaryEmail:{eq:$e}}}){edges{node{id deletedAt}}}}',
                    '{ companies(first:5){edges{node{id name}}} }',
                    'query Q { opportunities { edges { node { id stage } } } }'):
            validate_read_query(doc)

    def test_mutations_subscriptions_and_non_strings_rejected(self):
        for doc in ('mutation{deleteCompany(id:"x"){id}}', 'subscription{x}', '{a} mutation{b}', None, 42, '', '   '):
            self.assertTrue(rejected(doc), doc)

    def test_write_field_names_rejected_even_aliased_or_nested(self):
        for name in ('createNote', 'updateCompanies', 'deletePerson', 'destroyCompany', 'restoreCompany',
                     'upsertPerson', 'mergePeople', 'duplicateCompany'):
            self.assertTrue(rejected('query{ x: %s(id:"1"){id} }' % name), name)
            self.assertTrue(rejected('query{ a{ b{ %s{id} } } }' % name), name)

    def test_keywords_inside_strings_and_comments_do_not_trigger_or_hide_writes(self):
        validate_read_query('query($q:String){ people(filter:{name:{ilike:"mutation deleteX"}}){edges{node{id}}} } # mutation')
        self.assertTrue(rejected('query{ a } # harmless\n mutation{ deleteCompany(id:"1"){id} }'))

    def test_block_strings_refused_not_half_parsed(self):
        self.assertTrue(rejected('query{ a(x:"""deleteCompany""") }'))


if __name__ == '__main__':
    unittest.main()


class NoDeleteWrites(unittest.TestCase):
    def test_every_mutation_document_in_the_gtm_package_only_creates_or_updates(self):
        import re
        root = Path(__file__).resolve().parents[4] / 'gtm/gtm'
        verbs = []
        for path in root.rglob('*.py'):
            if path.name.startswith('test_') or path.name == 'twenty.py' and False:
                continue
            for line in path.read_text().splitlines():
                if 'mutation(' in line:
                    verbs += re.findall(r'\{\{?\s*([a-z]+)(?:[A-Z]|\{)', line.split('mutation(', 1)[1])
        self.assertTrue(verbs, 'no mutation documents found')
        self.assertLessEqual(set(verbs), {'create', 'update'}, verbs)
