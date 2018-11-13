from collections import defaultdict

from soynlp.lemmatizer import _lemma_candidate
from soynlp.lemmatizer import conjugate
from soynlp.noun import LRNounExtractor_v2
from soynlp.predicator import Predicator
from soynlp.predicator import PredicatorExtractor
from soynlp.tokenizer import MaxScoreTokenizer
from soynlp.utils import LRGraph


class NewsPOSExtractor:

    def __init__(self, verbose=True, ensure_normalized=True, extract_eomi=True):
        self._verbose = verbose
        self._ensure_normalized = ensure_normalized
        self._extract_eomi = extract_eomi

    def train(self, sents,
        # noun init
        min_num_of_noun_features=1, max_frequency_when_noun_is_eojeol=30,
        # noun extraction
        min_noun_score=0.3, min_noun_frequency=1, min_eojeol_frequency=1,
        # predicator train
        min_predicator_frequency=1,
        # Eomi extractor
        min_num_of_eomi_features=5, min_eomi_score=0.3, min_eomi_frequency=1,
        debug=False):

        self.nouns = self._extract_nouns(sents, min_num_of_noun_features,
            max_frequency_when_noun_is_eojeol, min_noun_score,
            min_noun_frequency, min_eojeol_frequency)

        self.adjectives, self.verbs = self._extract_predicators(
            sents, min_predicator_frequency, min_eojeol_frequency,
            min_num_of_eomi_features, min_eomi_score, min_eomi_frequency)

        self.adjective_stems = self.predicator_extractor._adjective_stems
        self.verb_stems = self.predicator_extractor._verb_stems
        self.eomis = self.predicator_extractor._eomis
        self.josas = self.predicator_extractor._josas

        self.eojeols = self.noun_extractor.lrgraph.to_EojeolCounter(reset_lrgraph=True)
        self.eojeols = self.eojeols._counter

    def _extract_nouns(self, sents,
        min_num_of_features=1, max_frequency_when_noun_is_eojeol=30, # noun init
        min_noun_score=0.3, min_noun_frequency=1, min_eojeol_frequency=1): # noun extraction

        self.noun_extractor = LRNounExtractor_v2(
            extract_pos_feature = False,
            extract_determiner = False,
            extract_compound = True,
            ensure_normalized = self._ensure_normalized,
            verbose = self._verbose,
            min_num_of_features = min_num_of_features,
            max_frequency_when_noun_is_eojeol = max_frequency_when_noun_is_eojeol
        )

        self.noun_extractor.train(sents, min_eojeol_frequency)
        nouns = self.noun_extractor.extract(min_noun_score,
            min_noun_frequency, reset_lrgraph=False)

        return nouns

    def _extract_predicators(self, sents,
        # predicator train
        min_predicator_frequency=1, min_eojeol_frequency=2,
        # Eomi extractor
        min_num_of_eomi_features=5, min_eomi_score=0.3, min_eomi_frequency=1):

        # prepare predicator_lrgraph
        predicator_lrgraph = LRGraph(self.noun_extractor.lrgraph._lr)
        noun_pos_features = {r for r in self.noun_extractor._pos_features}
        noun_pos_features.update({r for r in self.noun_extractor._common_features})

        # predicator extraction
        self.predicator_extractor = PredicatorExtractor(
            self.nouns,
            noun_pos_features,
            extract_eomi = self._extract_eomi,
            extract_stem = False,
            verbose = self._verbose
        )

        adjectives, verbs = self.predicator_extractor.train_extract(
            sents, min_eojeol_frequency, 100000, #filtering_checkpoint
            None, min_predicator_frequency, True, # filtering_checkpoint, lrgraph_reset
            min_num_of_eomi_features, min_eomi_score, min_eomi_frequency) # Eomi extractor

        return adjectives, verbs

    def extract(self):

        nouns, adjectives, verbs, josas, irrecognized = self._pattern_matching()

        adjectives = self._as_predicator(adjectives,
            self.adjectives, self.adjective_stems, self.eomis)
        verbs = self._as_predicator(verbs,
            self.verbs, self.verb_stems, self.eomis)

        wordtags = {
            'Noun': nouns,
            'Eomi': self.eomis,
            'Adjective': adjectives,
            'AdjectiveStem': self.adjective_stems,
            'Verb': verbs,
            'VerbStem': self.verb_stems,
            'Irrecognized': irrecognized
        }

        return wordtags

    def _as_predicator(self, counter, lemma_dict, stem, eomis):
        predicators = {}
        for word, count in counter.items():
            if word in lemma_dict:
                lemma = lemma_dict[word].lemma
            else:
                lemma = self._lemmatize(word, stem, eomis)
            predicators[word] = Predicator(count, lemma)
        return predicators

    def _match(self, word, reference):
        return word in reference

    def _separate_lr(self, word, lset, rset, begin=2):
        for i in range(begin, len(word)):
            l, r = word[:i], word[i:]
            if (l in lset) and (r in rset):
                return l, r
        return None

    def _cumulate_counter(self, counter, word_count):
        for word, count in word_count:
            counter[word] = counter.get(word, 0) + count
        return counter

    def _remove_recognized(self, eojeols, removals):
        return {word:count for word, count in eojeols.items() if not (word in removals)}

    def _lemmatize(self, word, stems, eomis):
        def only_knowns(lemmas):
            return [lemma for lemma in lemmas if
                    (lemma[0] in stems) and (lemma[1] in eomis)]

        for i in range(len(word) + 1, 0, -1):
            try:
                lemmas = _lemma_candidate(word[:i], word[i:])
            except:
                continue
            lemmas = only_knowns(lemmas)
            if lemmas:
                return lemmas
        return None

    def _parse_predicator_compounds(self, eojeols, predicators, base):
        stems = set()
        predicator_compounds = {}
        counter = {}
        for word, count in eojeols.items():
            lr = self._separate_lr(word, predicators, base)
            if lr is None:
                continue
            lemmas = base[lr[1]].lemma
            lemmas = {(word[0]+stem, eomi) for stem, eomi in lemmas}
            predicator_compounds[word] = Predicator(count, lemmas)
            stems.update({stem for stem, _ in lemmas})
            counter[word] = count
        return predicator_compounds, stems, counter

    def _pattern_matching(self):
        eojeols = self.eojeols
        total_frequency = sum(eojeols.values())

        # counter & match (Noun, Adjective, Verb)
        eojeols, nouns, adjectives, verbs = self._match_word(eojeols)

        eojeols, nouns, adjectives, verbs, josas = self._match_noun_and_word(
            eojeols, nouns, adjectives, verbs, {})

        eojeols, adjectives, verbs = self._match_predicator_compounds(
            eojeols, adjectives, verbs)

        eojeols, adjectives, verbs = self._lemmatizing_predicators(
            eojeols, adjectives, verbs)

        eojeols, nouns, adjectives, verbs, josas = self._match_syllable_noun_and_r(
            eojeols, nouns, adjectives, verbs, josas)

        eojeols = self._remove_irregular_words(eojeols)

        eojeols, nouns = self._match_compound_nouns(
            eojeols, nouns, adjectives, verbs, josas)

        if self._verbose:
            self._print_stats(total_frequency,
                nouns, adjectives, verbs, josas, eojeols)

        return nouns, adjectives, verbs, josas, eojeols

    def _match_word(self, eojeols):
        if self._verbose:
            print('    - matching "Noun, Adjectives, and Verbs" from {} eojeols'.format(len(eojeols)))

        nouns = {word:count for word, count in eojeols.items() if self._match(word, self.nouns)}
        adjectives = {word:count for word, count in eojeols.items() if self._match(word, self.adjectives)}
        verbs = {word:count for word, count in eojeols.items() if self._match(word, self.verbs)}

        for removals in (nouns, adjectives, verbs):
            eojeols = self._remove_recognized(eojeols, removals)
        return eojeols, nouns, adjectives, verbs

    def _match_noun_and_word(self, eojeols, nouns, adjectives, verbs, josas):
        if self._verbose:
            print('    - matching "Noun + Josa/Adjective/Verb" from {} eojeols'.format(len(eojeols)))

        def match_process(eojeols, nouns, rset, rcounter, removals):
            noun_r = [(self._separate_lr(word, nouns, rset), count) for word, count in eojeols.items()]
            noun_r = [(word, count) for word, count in noun_r if word is not None]
            nouns = self._cumulate_counter(nouns, [(word[0], count) for word, count in noun_r])
            rcounter = self._cumulate_counter(rcounter, [(word[1], count) for word, count in noun_r])
            removals.update({''.join(word) for word, _ in noun_r})
            return nouns, rcounter, removals

        nouns, josas, removals = match_process(eojeols, nouns, self.josas, {}, set())
        nouns, adjectives, removals = match_process(eojeols, nouns, adjectives, adjectives, removals)
        nouns, verbs, removals = match_process(eojeols, nouns, verbs, verbs, removals)
        eojeols = self._remove_recognized(eojeols, removals)

        return eojeols, nouns, adjectives, verbs, josas

    def _match_predicator_compounds(self, eojeols, adjectives, verbs):
        if self._verbose:
            print('    - matching "Predicator + Adjective/Verb" from {} eojeols'.format(len(eojeols)))

        predicators = set(adjectives.keys())
        predicators.update(set(verbs.keys()))

        compounds, stems, counter = self._parse_predicator_compounds(
            eojeols, predicators, self.adjectives)
        self.adjectives.update(compounds)
        self.adjective_stems.update(stems)
        adjectives = self._cumulate_counter(adjectives, counter.items())

        ## verb compounds
        compounds, stems, counter = self._parse_predicator_compounds(
            eojeols, predicators, self.verbs)
        self.verbs.update(compounds)
        self.verb_stems.update(stems)
        verbs = self._cumulate_counter(verbs, counter.items())

        ## remove matched eojeols
        removals = {word for word in adjectives}
        removals.update({word for word in verbs})
        eojeols = self._remove_recognized(eojeols, removals)

        return eojeols, adjectives, verbs

    def _lemmatizing_predicators(self, eojeols, adjectives, verbs):
        def lemmatize(stems, eomis, eojeols, verbose=True):
            predicator = {}
            n = len(eojeols)
            for i, (word, count) in enumerate(eojeols.items()):
                if verbose and i % 1000 == 0:
                    print('\r    - lemmatizing {} / {}'.format(i, n), end='')
                lemmas = self._lemmatize(word, stems, eomis)
                if lemmas is None:
                    continue
                predicator[word] = Predicator(count, lemmas)
            return predicator

        if self._verbose:
            print('    - lemmatizing Adjective/Verb from {} eojeols'.format(len(eojeols)))

        new_adjectives = lemmatize(self.adjective_stems, self.eomis, eojeols, self._verbose)
        counter_adj = {word:count for word, count in eojeols.items() if word in new_adjectives}
        adjectives = self._cumulate_counter(adjectives, counter_adj.items())
        self.adjectives.update(new_adjectives)

        new_verbs = lemmatize(self.adjective_stems, self.eomis, eojeols, self._verbose)
        counter_verb = {word:count for word, count in eojeols.items() if word in new_adjectives}
        verbs = self._cumulate_counter(verbs, counter_verb.items())
        self.verbs.update(new_verbs)

        eojeols = self._remove_recognized(eojeols, counter_adj)
        eojeols = self._remove_recognized(eojeols, counter_verb)

        return eojeols, adjectives, verbs

    def _match_syllable_noun_and_r(self, eojeols, nouns, adjectives, verbs, josas):
        def syllable_noun_and_r(rcounter, removals):
            for word, count in eojeols.items():
                l, r = word[:1], word[1:]
                if r in rcounter:
                    nouns[l] = nouns.get(l, 0) + count
                    rcounter[r] = rcounter.get(r, 0) + count
                    removals.add(word)
            return removals

        if self._verbose:
            print('\r    - parse 1 syllable Noun + Adj/Verb/Josa from {} eojeols'.format(len(eojeols)))

        removals = syllable_noun_and_r(adjectives, set())
        removals = syllable_noun_and_r(verbs, removals)
        removals = syllable_noun_and_r(josas, removals)

        eojeols = self._remove_recognized(eojeols, removals)

        return eojeols, nouns, adjectives, verbs, josas

    def _remove_irregular_words(self, eojeols):
        def remove(word):
            return (word in self.josas) or (word in self.eomis) or (len(word) == 1)
        return {word:count for word, count in eojeols.items() if not remove(word)}

    def _match_compound_nouns(self, eojeols, nouns, adjectives, verbs, josas):
        if self._verbose:
            print('    - extract compound nouns from {} eojeols'.format(len(eojeols)))

        suffix = {word for word in nouns}
        suffix.update({word for word in adjectives})
        suffix.update({word for word in verbs})
        suffix.update({word for word in josas})

        compounds, removals = self._extract_compound_nouns(eojeols, nouns, suffix)
        nouns = self._cumulate_counter(nouns, compounds.items())
        eojeols = self._remove_recognized(eojeols, removals)

        return eojeols, nouns

    def _print_stats(self, total_frequency, nouns, adjectives, verbs, josas, eojeols):
        print('\n[POS Extractor] ## statistics')

        as_percent = lambda dic: 100 * sum(dic.values()) / total_frequency

        stats = [
            (len(nouns), as_percent(nouns), 'Noun + [Josa/Predicator]'),
            (len(adjectives), as_percent(adjectives), '[Noun] + Adjective'),
            (len(verbs), as_percent(verbs), '[Noun] + Verb'),
            (len(josas), as_percent(josas), '[Noun] + Josas'),
            (len(eojeols), as_percent(eojeols), 'Irrecognizable')
        ]
        for args in stats:
            print('[POS Extractor] ({}, {:.3f} %) words in {}'.format(*args))

    def _extract_compound_nouns(self, eojeols, nouns, suffix):
        def parse_compound(tokens):
            for token in tokens[:-1]:
                if token[3] <= 0:
                    return None
            # Noun* + Josa
            if len(tokens) >= 3 and (tokens[-1][0] in suffix):
                return ''.join(t[0] for t in tokens[:-1])
            # all tokens are noun
            if tokens[-1][3] > 0:
                return ''.join(t[0] for t in tokens)
            # else, not compound
            return None

        tokenizer = MaxScoreTokenizer(scores = {noun:1 for noun in nouns if len(noun) > 1})

        compounds, removals = {}, set()
        for word, count in eojeols.items():
            # format: [(word, begin, end, score, length)]
            tokens = tokenizer.tokenize(word, flatten=False)[0]
            noun = parse_compound(tokens)
            if noun is not None:
                compounds[noun] = compounds.get(noun, 0) + count
                removals.add(word)

        return compounds, removals