import pickle
from collections import defaultdict

import igraph as ig
import ipdb
from colbert import Searcher
from colbert.data import Queries
from colbert.infra import RunConfig, Run, ColBERTConfig
from langchain_community.chat_models import ChatOllama

from named_entity_extraction_parallel import *
from processing import *
import os
from glob import glob
from transformers import AutoModel, AutoTokenizer
import json
import torch

os.environ['TOKENIZERS_PARALLELISM'] = 'FALSE'

COLBERT_CKPT_DIR = "exp/colbertv2.0"


def min_max_normalize(x):
    return (x - np.min(x)) / (np.max(x) - np.min(x))


class HippoRAG():

    def __init__(self, corpus_name='hotpotqa', extraction_model='openai', extraction_model_name='gpt-3.5-turbo-1106', retrieval_model_name='facebook/contriever',
                 extraction_type='ner', graph_type='facts_and_sim', sim_threshold=0.8, node_specificity=True, doc_ensemble=False,
                 colbert_config=None, dpr_only=False, graph_alg='ppr', damping=0.1, recognition_threshold=0.9):
        """
        @param corpus_name: Name of the dataset to use for retrieval
        @param extraction_model_name: LLM used for query NER
        @param retrieval_model_name: Retrieval encoder used to link query named entities with query nodes
        @param extraction_type: Type of NER extraction during indexing
        @param graph_type: Type of graph used by HippoRAG
        @param sim_threshold: Synonymy threshold which was used to create the graph that will be used by HippoRAG
        @param node_specificity: Flag that determines whether node specificity will be used
        @param doc_ensemble: Flag to determine whether to use uncertainty-based ensembling
        @param colbert_config: ColBERTv2 configuration
        @param dpr_only: Flag to determine whether HippoRAG will be used at all
        @param graph_alg: Type of graph algorithm to be used for retrieval, defaults ot PPR
        @param damping: Damping factor for PPR
        @param recognition_threshold: Threshold used for uncertainty-based ensembling.
        """

        self.corpus_name = corpus_name
        self.extraction_model_name = extraction_model_name
        self.extraction_model_name_processed = extraction_model_name.replace('/', '_')
        self.client = init_langchain_model(extraction_model, extraction_model_name)
        self.retrieval_model_name = retrieval_model_name  # 'colbertv2', 'facebook/contriever', or other HuggingFace models
        self.retrieval_model_name_processed = retrieval_model_name.replace('/', '_').replace('.', '')

        self.extraction_type = extraction_type
        self.graph_type = graph_type
        self.phrase_type = 'ents_only_lower_preprocess'
        self.sim_threshold = sim_threshold
        self.node_specificity = node_specificity
        if colbert_config is None:
            self.colbert_config = {'root': f'data/lm_vectors/colbert/{corpus_name}',
                                   'doc_index_name': 'nbits_2', 'phrase_index_name': 'nbits_2'}
        else:
            self.colbert_config = colbert_config  # a dict, 'root', 'doc_index_name', 'phrase_index_name'

        self.graph_alg = graph_alg
        self.damping = damping
        self.recognition_threshold = recognition_threshold

        self.version = 'v3'

        try:
            self.named_entity_cache = pd.read_csv('output/{}_queries.named_entity_output.tsv'.format(self.corpus_name), sep='\t')
        except:
            self.named_entity_cache = pd.DataFrame([], columns=['query', 'triples'])

        if 'query' in self.named_entity_cache:
            self.named_entity_cache = {row['query']: eval(row['triples']) for i, row in
                                       self.named_entity_cache.iterrows()}
        elif 'question' in self.named_entity_cache:
            self.named_entity_cache = {row['question']: eval(row['triples']) for i, row in self.named_entity_cache.iterrows()}

        if self.retrieval_model_name not in ['colbertv2', 'bm25']:
            self.retrieval_model = AutoModel.from_pretrained(self.retrieval_model_name).to('cuda')
            self.tokenizer = AutoTokenizer.from_pretrained(self.retrieval_model_name)

        # Loading Important Corpus Files
        self.load_important_files()

        # Construct Graph
        self.build_graph()

        # Loading Node Embeddings
        self.load_node_vectors()

        self.doc_ensemble = doc_ensemble
        self.dpr_only = dpr_only

        if (doc_ensemble or dpr_only) and self.retrieval_model_name not in ['colbertv2', 'bm25']:
            # Loading Doc Embeddings
            self.get_dpr_doc_embedding()

        if self.retrieval_model_name == 'colbertv2':
            with Run().context(RunConfig(nranks=1, experiment="phrase", root=self.colbert_config['root'])):
                config = ColBERTConfig(root=self.colbert_config['root'], )
                self.phrase_searcher = Searcher(index=self.colbert_config['phrase_index_name'], config=config, verbose=0)
            if self.doc_ensemble or dpr_only:
                with Run().context(RunConfig(nranks=1, experiment="corpus", root=self.colbert_config['root'])):
                    config = ColBERTConfig(root=self.colbert_config['root'], )
                    self.corpus_searcher = Searcher(index=self.colbert_config['doc_index_name'], config=config, verbose=0)

        self.statistics = {}
        self.ensembling_debug = []

    def rank_docs(self, query: str, top_k=10):
        """
        Rank documents based on the query
        :param query: the input phrase
        :param damping: damping factor for the pagerank algorithm, i.e., the probability of jumping to a random node
        :param top_k: the number of documents to return
        :return: the ranked document ids and their scores
        """

        if self.dpr_only:
            query_ner_list = []
        else:
            # Extract Entities
            try:
                if query in self.named_entity_cache:
                    query_ner_list = self.named_entity_cache[query]['named_entities']
                else:
                    query_ner_json, total_tokens = self.named_entity_recognition(query)
                    query_ner_list = eval(query_ner_json)['named_entities']

                query_ner_list = [processing_phrases(p) for p in query_ner_list]
            except:
                print('Error in Query NER')
                query_ner_list = []

        if 'colbertv2' in self.retrieval_model_name:
            # Get Query Doc Scores
            if self.doc_ensemble or self.dpr_only:
                query_doc_scores = np.zeros(self.doc_to_phrases_mat.shape[0])

                queries = Queries(path=None, data={0: query})
                ranking = self.corpus_searcher.search_all(queries, k=self.doc_to_phrases_mat.shape[0])

                max_query_score = self.get_colbert_max_score(query)

                num_docs = 0
                for doc_id, rank, score in ranking.data[0]:
                    query_doc_scores[doc_id] = score

            if len(query_ner_list) > 0:  # if no entities are found, assign uniform probability to documents
                top_phrase_vectors, top_phrase_scores = self.get_top_phrase_vec_colbertv2(query_ner_list)
        else:  # DPR model
            # Get Query Doc Scores
            if self.doc_ensemble or self.dpr_only:
                query_embedding = self.get_embedding_with_mean_pooling(query).cpu().numpy()
                query_doc_scores = np.dot(self.doc_embedding_mat, query_embedding.T)
                query_doc_scores = query_doc_scores.T[0]

            if len(query_ner_list) > 0:  # if no entities are found, assign uniform probability to documents
                top_phrase_vectors, top_phrase_scores = self.get_top_phrase_vec_dpr(query_ner_list)

        # Run Personalized PageRank (PPR) or other Graph Alg Doc Scores
        if len(query_ner_list) > 0:
            combined_vector = np.max([top_phrase_vectors], axis=0)

            if self.graph_alg == 'ppr':
                ppr_phrase_probs = self.run_pagerank_igraph_chunk([top_phrase_vectors])[0]
            elif self.graph_alg == 'none':
                ppr_phrase_probs = combined_vector
            elif self.graph_alg == 'neighbor_2':
                ppr_phrase_probs = self.get_neighbors(combined_vector, 2)
            elif self.graph_alg == 'neighbor_3':
                ppr_phrase_probs = self.get_neighbors(combined_vector, 3)
            elif self.graph_alg == 'paths':
                ppr_phrase_probs = self.get_neighbors(combined_vector, 3)
            else:
                assert False, f'Graph Algorithm {self.graph_alg} Not Implemented'

            fact_prob = self.facts_to_phrases_mat.dot(ppr_phrase_probs)
            ppr_doc_prob = self.docs_to_facts_mat.dot(fact_prob)
            ppr_doc_prob = min_max_normalize(ppr_doc_prob)
        else:
            ppr_doc_prob = np.ones(len(self.extracted_triples)) / len(self.extracted_triples)

        # Combine Query-Doc and PPR Scores
        if self.doc_ensemble or self.dpr_only:
            # doc_prob = ppr_doc_prob * 0.5 + min_max_normalize(query_doc_scores) * 0.5
            if len(query_ner_list) == 0:
                doc_prob = query_doc_scores
                self.statistics['doc'] = self.statistics.get('doc', 0) + 1
            elif np.min(list(top_phrase_scores.values())) > self.recognition_threshold:  # high confidence in named entities
                doc_prob = ppr_doc_prob
                self.statistics['ppr'] = self.statistics.get('ppr', 0) + 1
            else:  # relatively low confidence in named entities, combine the two scores
                # the higher threshold, the higher chance to use the doc ensemble
                doc_prob = ppr_doc_prob * 0.5 + min_max_normalize(query_doc_scores) * 0.5
                query_doc_scores = min_max_normalize(query_doc_scores)

                top_ppr = np.argsort(ppr_doc_prob)[::-1][:10]
                top_ppr = [(top, ppr_doc_prob[top]) for top in top_ppr]

                top_doc = np.argsort(query_doc_scores)[::-1][:10]
                top_doc = [(top, query_doc_scores[top]) for top in top_doc]

                top_hybrid = np.argsort(doc_prob)[::-1][:10]
                top_hybrid = [(top, doc_prob[top]) for top in top_hybrid]

                self.ensembling_debug.append((top_ppr, top_doc, top_hybrid))
                self.statistics['ppr_doc_ensemble'] = self.statistics.get('ppr_doc_ensemble', 0) + 1
        else:
            doc_prob = ppr_doc_prob

        # Return ranked docs and ranked scores
        sorted_doc_ids = np.argsort(doc_prob, kind='mergesort')[::-1]
        sorted_scores = doc_prob[sorted_doc_ids]

        if not (self.dpr_only) and len(query_ner_list) > 0:
            # logs
            phrase_one_hop_triples = []
            for phrase_id in np.where(top_phrase_vectors > 0)[0]:
                # get all the triples that contain the phrase from self.graph_plus
                for t in list(self.kg_adj_list[phrase_id].items())[:20]:
                    phrase_one_hop_triples.append([self.phrases[t[0]], t[1]])
                for t in list(self.kg_inverse_adj_list[phrase_id].items())[:20]:
                    phrase_one_hop_triples.append([self.phrases[t[0]], t[1], 'inv'])

            # get top ranked nodes from doc_prob and self.doc_to_phrases_mat
            nodes_in_retrieved_doc = []
            for doc_id in sorted_doc_ids[:5]:
                node_id_in_doc = list(np.where(self.doc_to_phrases_mat[[doc_id], :].toarray()[0] > 0)[0])
                nodes_in_retrieved_doc.append([self.phrases[node_id] for node_id in node_id_in_doc])

            # get top ppr_phrase_probs
            top_pagerank_phrase_ids = np.argsort(ppr_phrase_probs, kind='mergesort')[::-1][:20]

            # get phrases for top_pagerank_phrase_ids
            top_ranked_nodes = [self.phrases[phrase_id] for phrase_id in top_pagerank_phrase_ids]
            logs = {'named_entities': query_ner_list, 'linked_node_scores': [list(k) + [float(v)] for k, v in top_phrase_scores.items()],
                    '1-hop_graph_for_linked_nodes': phrase_one_hop_triples,
                    'top_ranked_nodes': top_ranked_nodes, 'nodes_in_retrieved_doc': nodes_in_retrieved_doc}
        else:
            logs = {}

        return sorted_doc_ids.tolist()[:top_k], sorted_scores.tolist()[:top_k], logs

    def get_neighbors(self, prob_vector, max_depth=1):

        initial_nodes = prob_vector.nonzero()[0]
        min_prob = np.min(prob_vector[initial_nodes])

        for initial_node in initial_nodes:
            all_neighborhood = []

            current_nodes = [initial_node]

            for depth in range(max_depth):
                next_nodes = []

                for node in current_nodes:
                    next_nodes.extend(self.g.neighbors(node))
                    all_neighborhood.extend(self.g.neighbors(node))

                current_nodes = list(set(next_nodes))

            for i in set(all_neighborhood):
                prob_vector[i] += 0.5 * min_prob

        return prob_vector

    def get_neighbors(self, prob_vector, max_depth=4):

        initial_nodes = prob_vector.nonzero()[0]
        min_prob = np.min(prob_vector[initial_nodes])

        for initial_node in initial_nodes:
            all_neighborhood = []

            current_nodes = [initial_node]

            for depth in range(max_depth):
                next_nodes = []

                for node in current_nodes:
                    next_nodes.extend(self.g.neighbors(node))
                    all_neighborhood.extend(self.g.neighbors(node))

                current_nodes = list(set(next_nodes))

            for i in set(all_neighborhood):
                prob_vector[i] += 0.5 * min_prob

        return prob_vector

    def load_important_files(self):
        possible_files = glob(
            'output/openie_{}_results_{}_{}_*.json'.format(self.corpus_name, self.extraction_type, self.extraction_model_name_processed))
        max_samples = np.max(
            [int(file.split('{}_'.format(self.extraction_model_name_processed))[1].split('.json')[0]) for file in possible_files])
        extracted_file = json.load(open(
            'output/openie_{}_results_{}_{}_{}.json'.format(self.corpus_name, self.extraction_type, self.extraction_model_name_processed, max_samples),
            'r'))

        self.extracted_triples = extracted_file['docs']

        if self.extraction_model_name != 'gpt-3.5-turbo-1106':
            self.extraction_type = self.extraction_type + '_' + self.extraction_model_name_processed

        if self.corpus_name == 'hotpotqa':
            self.dataset_df = pd.DataFrame([p['passage'].split('\n')[0] for p in self.extracted_triples])
            self.dataset_df['paragraph'] = [s['passage'] for s in self.extracted_triples]
        if self.corpus_name == 'hotpotqa_train':
            self.dataset_df = pd.DataFrame([p['passage'].split('\n')[0] for p in self.extracted_triples])
            self.dataset_df['paragraph'] = [s['passage'] for s in self.extracted_triples]
        elif 'musique' in self.corpus_name:
            self.dataset_df = pd.DataFrame([p['passage'] for p in self.extracted_triples])
            self.dataset_df['paragraph'] = [s['passage'] for s in self.extracted_triples]
        elif self.corpus_name == '2wikimultihopqa':
            self.dataset_df = pd.DataFrame([p['passage'] for p in self.extracted_triples])
            self.dataset_df['paragraph'] = [s['passage'] for s in self.extracted_triples]
            self.dataset_df['title'] = [s['title'] for s in self.extracted_triples]
        elif 'case_study' in self.corpus_name:
            self.dataset_df = pd.DataFrame([p['passage'] for p in self.extracted_triples])
            self.dataset_df['paragraph'] = [s['passage'] for s in self.extracted_triples]

        self.kb_phrase_dict = pickle.load(open(
            'output/{}_{}_graph_phrase_dict_{}_{}.{}.subset.p'.format(self.corpus_name, self.graph_type, self.phrase_type,
                                                                      self.extraction_type, self.version), 'rb'))
        self.lose_fact_dict = pickle.load(open(
            'output/{}_{}_graph_fact_dict_{}_{}.{}.subset.p'.format(self.corpus_name, self.graph_type, self.phrase_type,
                                                                    self.extraction_type, self.version), 'rb'))

        try:
            self.relations_dict = pickle.load(open(
                'output/{}_{}_graph_relation_dict_{}_{}_{}.{}.subset.p'.format(
                    self.corpus_name, self.graph_type, self.phrase_type,
                    self.extraction_type, self.retrieval_model_name_processed, self.version), 'rb'))
        except:
            self.relations_dict = pickle.load(open('output/{}_{}_graph_relation_dict_{}_{}.{}.subset.p'.format(
                self.corpus_name, self.graph_type, self.phrase_type,
                self.extraction_type, self.retrieval_model_name_processed,
                self.version), 'rb'))

        self.lose_facts = list(self.lose_fact_dict.keys())
        self.lose_facts = [self.lose_facts[i] for i in np.argsort(list(self.lose_fact_dict.values()))]
        self.phrases = np.array(list(self.kb_phrase_dict.keys()))[np.argsort(list(self.kb_phrase_dict.values()))]

        self.docs_to_facts = pickle.load(open(
            'output/{}_{}_graph_doc_to_facts_{}_{}.{}.subset.p'.format(self.corpus_name, self.graph_type, self.phrase_type,
                                                                       self.extraction_type, self.version), 'rb'))
        self.facts_to_phrases = pickle.load(open(
            'output/{}_{}_graph_facts_to_phrases_{}_{}.{}.subset.p'.format(self.corpus_name, self.graph_type, self.phrase_type,
                                                                           self.extraction_type, self.version), 'rb'))

        self.docs_to_facts_mat = pickle.load(
            open(
                'output/{}_{}_graph_doc_to_facts_csr_{}_{}.{}.subset.p'.format(self.corpus_name, self.graph_type, self.phrase_type,
                                                                               self.extraction_type, self.version),
                'rb'))  # (num docs, num facts)
        self.facts_to_phrases_mat = pickle.load(open(
            'output/{}_{}_graph_facts_to_phrases_csr_{}_{}.{}.subset.p'.format(self.corpus_name, self.graph_type, self.phrase_type,
                                                                               self.extraction_type, self.version),
            'rb'))  # (num facts, num phrases)

        self.doc_to_phrases_mat = self.docs_to_facts_mat.dot(self.facts_to_phrases_mat)
        self.doc_to_phrases_mat[self.doc_to_phrases_mat.nonzero()] = 1
        self.phrase_to_num_doc = self.doc_to_phrases_mat.sum(0).T

        graph_file_path = 'output/{}_{}_graph_mean_{}_thresh_{}_{}_{}.{}.subset.p'.format(self.corpus_name, self.graph_type,
                                                                                          str(self.sim_threshold), self.phrase_type,
                                                                                          self.extraction_type,
                                                                                          self.retrieval_model_name_processed,
                                                                                          self.version)
        if os.path.isfile(graph_file_path):
            self.graph_plus = pickle.load(open(graph_file_path, 'rb'))  # (phrase1 id, phrase2 id) -> the number of occurrences

    def get_phrases_in_doc_str(self, doc: str):
        # find doc id from self.dataset_df
        try:
            doc_id = self.dataset_df[self.dataset_df.paragraph == doc].index[0]
            phrase_ids = self.doc_to_phrases_mat[[doc_id], :].nonzero()[1].tolist()
            return [self.phrases[phrase_id] for phrase_id in phrase_ids]
        except:
            return []

    def build_graph(self):

        edges = set()

        new_graph_plus = {}
        self.kg_adj_list = defaultdict(dict)
        self.kg_inverse_adj_list = defaultdict(dict)

        for edge, weight in tqdm(self.graph_plus.items(), total=len(self.graph_plus), desc='Building Graph'):
            edge1 = edge[0]
            edge2 = edge[1]

            if (edge1, edge2) not in edges and edge1 != edge2:
                new_graph_plus[(edge1, edge2)] = self.graph_plus[(edge[0], edge[1])]
                edges.add((edge1, edge2))
                self.kg_adj_list[edge1][edge2] = self.graph_plus[(edge[0], edge[1])]
                self.kg_inverse_adj_list[edge2][edge1] = self.graph_plus[(edge[0], edge[1])]

        self.graph_plus = new_graph_plus

        edges = list(edges)

        n_vertices = len(self.kb_phrase_dict)
        self.g = ig.Graph(n_vertices, edges)

        self.g.es['weight'] = [self.graph_plus[(v1, v3)] for v1, v3 in edges]
        print('Graph built: num vertices:', n_vertices, 'num edges:', len(edges))

    def load_node_vectors(self):
        print('## Loading Node Vectors for DPR Only')

        if 'colbertv2' in self.retrieval_model_name:
            ranking_filename = 'output/{}_ranking_dict_{}_{}_{}.{}.p'.format(self.corpus_name, self.phrase_type,
                                                                             self.extraction_type, self.retrieval_model_name_processed,
                                                                             self.version)

            if os.path.exists(ranking_filename):
                colbert_ranking_dict = pickle.load(open(ranking_filename, 'rb'))
            else:
                pass
                # using colbertv2_indexing.py for now
        else:
            kb_vectors = []
            self.strings = open('data/lm_vectors/{}_mean/encoded_strings.txt'.format(self.retrieval_model_name_processed),
                                'r').readlines()

            for i in range(len(glob('data/lm_vectors/{}_mean/vecs_*'.format(self.retrieval_model_name_processed)))):
                kb_vectors.append(
                    torch.Tensor(pickle.load(
                        open('data/lm_vectors/{}_mean/vecs_{}.p'.format(self.retrieval_model_name_processed, i), 'rb'))))

            kb_mat = torch.cat(kb_vectors)  # a matrix of phrase vectors

            self.strings = [s.strip() for s in self.strings]
            self.string_to_id = {string: i for i, string in enumerate(self.strings)}

            kb_mat = kb_mat.T.divide(torch.linalg.norm(kb_mat, dim=1)).T
            kb_mat = kb_mat.to('cuda')

            kb_only_indices = []

            num_non_vector_phrases = 0

            for i in range(len(self.kb_phrase_dict)):
                phrase = self.phrases[i]
                if phrase not in self.string_to_id:
                    num_non_vector_phrases += 1

                phrase_id = self.string_to_id.get(phrase, 0)
                kb_only_indices.append(phrase_id)

            self.kb_only_mat = kb_mat[kb_only_indices]  # a matrix of phrase vectors

            print('{} phrases did not have vectors.'.format(num_non_vector_phrases))

    def get_dpr_doc_embedding(self):
        cache_filename = 'data/lm_vectors/{}_mean/{}_doc_embeddings.p'.format(self.retrieval_model_name_processed,
                                                                              self.corpus_name)
        if os.path.exists(cache_filename):
            self.doc_embedding_mat = pickle.load(open(cache_filename, 'rb'))
            print(f'Loaded doc embeddings from {cache_filename}, shape: {self.doc_embedding_mat.shape}')
        else:
            self.doc_embeddings = []

            for doc in tqdm(self.dataset_df.itertuples(index=False), total=len(self.dataset_df),
                            desc='Embedding Documents'):
                embeddings = self.get_embedding_with_mean_pooling(doc.paragraph)
                self.doc_embeddings.append(embeddings.cpu().numpy())

            self.doc_embedding_mat = np.concatenate(self.doc_embeddings, axis=0)  # (num docs, embedding dim)
            pickle.dump(self.doc_embedding_mat, open(cache_filename, 'wb'))
            print(f'Saved doc embeddings to {cache_filename}, shape: {self.doc_embedding_mat.shape}')

    def get_embedding_with_mean_pooling(self, input_str):
        with torch.no_grad():
            encoding = self.tokenizer(input_str, return_tensors='pt', padding=True, truncation=True)
            input_ids = encoding['input_ids']
            attention_mask = encoding['attention_mask']
            input_ids = input_ids.to('cuda')
            attention_mask = attention_mask.to('cuda')
            outputs = self.retrieval_model(input_ids, attention_mask=attention_mask)
            embeddings = mean_pooling(outputs[0], attention_mask)
            embeddings = embeddings.T.divide(torch.linalg.norm(embeddings, dim=1)).T

            return embeddings

    def run_pagerank_igraph_chunk(self, reset_prob_chunk):
        """
        Run pagerank on the graph
        :param reset_prob_chunk:
        :return: PageRank probabilities
        """
        pageranked_probabilities = []

        for reset_prob in tqdm(reset_prob_chunk, desc='pagerank chunk'):
            pageranked_probs = self.g.personalized_pagerank(vertices=range(len(self.kb_phrase_dict)), damping=self.damping, directed=False,
                                                            weights='weight', reset=reset_prob, implementation='prpack')

            pageranked_probabilities.append(np.array(pageranked_probs))

        return np.array(pageranked_probabilities)

    def get_top_phrase_vec_dpr(self, query_ner_list: list):
        """
        Get the most similar phrases (as vector) in the KG given the named entities
        :param query_ner_list:
        :return:
        """
        query_ner_embeddings = self.get_embedding_with_mean_pooling(query_ner_list)

        # Get Closest Entity Nodes
        prob_vectors = query_ner_embeddings.matmul(self.kb_only_mat.T).cpu().numpy()

        phrase_ids = []
        max_scores = []

        for prob_vector in prob_vectors:
            phrase_id = np.argmax(prob_vector)
            phrase_ids.append(phrase_id)
            max_scores.append(prob_vector[phrase_id])

        # create a vector (num_phrase) with 1s at the indices of the linked phrases and 0s elsewhere
        # if node_specificity is True, it's not one-hot but a weight
        top_phrase_vec = np.zeros(len(self.phrases))

        for phrase_id in phrase_ids:
            if self.node_specificity:
                if self.phrase_to_num_doc[phrase_id] == 0:  # just in case the phrase is not recorded in any documents
                    weight = 1
                else:  # the more frequent the phrase, the less weight it gets
                    weight = 1 / self.phrase_to_num_doc[phrase_id]

                top_phrase_vec[phrase_id] = weight
            else:
                top_phrase_vec[phrase_id] = 1.0

        return top_phrase_vec, {(query, self.phrases[phrase_id]): max_score for phrase_id, max_score, query in zip(phrase_ids, max_scores, query_ner_list)}

    def get_colbert_max_score(self, query):
        queries_ = [query]
        encoded_query = self.phrase_searcher.encode(queries_, full_length_search=False)
        encoded_doc = self.phrase_searcher.checkpoint.docFromText(queries_).float()
        max_score = encoded_query[0].matmul(encoded_doc[0].T).max(dim=1).values.sum().detach().cpu().numpy()

        return max_score

    def get_colbert_real_score(self, query, doc):
        queries_ = [query]
        encoded_query = self.phrase_searcher.encode(queries_, full_length_search=False)

        docs_ = [doc]
        encoded_doc = self.phrase_searcher.checkpoint.docFromText(docs_).float()

        real_score = encoded_query[0].matmul(encoded_doc[0].T).max(dim=1).values.sum().detach().cpu().numpy()

        return real_score

    def get_top_phrase_vec_colbertv2(self, query_ner_list):
        phrase_ids = []
        max_scores = []

        for query in query_ner_list:
            queries = Queries(path=None, data={0: query})

            queries_ = [query]
            encoded_query = self.phrase_searcher.encode(queries_, full_length_search=False)

            max_score = self.get_colbert_max_score(query)

            ranking = self.phrase_searcher.search_all(queries, k=1)
            for phrase_id, rank, score in ranking.data[0]:
                phrase = self.phrases[phrase_id]
                phrases_ = [phrase]
                encoded_doc = self.phrase_searcher.checkpoint.docFromText(phrases_).float()
                real_score = encoded_query[0].matmul(encoded_doc[0].T).max(dim=1).values.sum().detach().cpu().numpy()

                phrase_ids.append(phrase_id)
                max_scores.append(real_score / max_score)

        # create a vector (num_doc) with 1s at the indices of the retrieved documents and 0s elsewhere
        top_phrase_vec = np.zeros(len(self.phrases))

        for phrase_id in phrase_ids:
            if self.node_specificity:
                if self.phrase_to_num_doc[phrase_id] == 0:
                    weight = 1
                else:
                    weight = 1 / self.phrase_to_num_doc[phrase_id]
                top_phrase_vec[phrase_id] = weight
            else:
                top_phrase_vec[phrase_id] = 1.0

        return top_phrase_vec, {(query, self.phrases[phrase_id]): max_score for phrase_id, max_score, query in zip(phrase_ids, max_scores, query_ner_list)}

    def named_entity_recognition(self, text: str):
        query_ner_prompts = ChatPromptTemplate.from_messages([SystemMessage("You're a very effective entity extraction system."),
                                                              HumanMessage(query_prompt_one_shot_input),
                                                              AIMessage(query_prompt_one_shot_output),
                                                              HumanMessage(query_prompt_template.format(text))])
        query_ner_messages = query_ner_prompts.format_prompt()
        json_mode = False
        if isinstance(client, ChatOpenAI):  # JSON mode
            chat_completion = client.invoke(query_ner_messages.to_messages(), temperature=0, max_tokens=300, stop=['\n\n'], response_format={"type": "json_object"})
            response_content = chat_completion.content
            total_tokens = chat_completion.response_metadata['token_usage']['total_tokens']
            json_mode = True
        elif isinstance(client, ChatOllama):
            response_content = client.invoke(query_ner_messages.to_messages())
        else:  # no JSON mode
            chat_completion = client.invoke(query_ner_messages.to_messages(), temperature=0, max_tokens=300, stop=['\n\n'])
            response_content = chat_completion.content
            response_content = extract_json_dict(response_content)
            total_tokens = chat_completion.response_metadata['token_usage']['total_tokens']

        if not json_mode:
            try:
                assert 'named_entities' in response_content
                response_content = str(response_content)
            except Exception as e:
                print('Query NER exception', e)
                response_content = {'named_entities': []}

        return response_content, total_tokens


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=str)
    parser.add_argument('--llm', type=str, default='openai', help="LLM, e.g., 'openai' or 'together'")
    parser.add_argument('--extraction_model', type=str, default='gpt-3.5-turbo-1106')
    parser.add_argument('--retrieval_model', type=str, choices=['facebook/contriever', 'colbertv2'])
    parser.add_argument('--doc_ensemble', type=bool, action='store_true')
    args = parser.parse_args()

    hipporag = HippoRAG(args.dataset_name, args.llm, args.extraction_model, args.retrieval_model, doc_ensemble=args.doc_ensemble)

    queries = ["Which Stanford University professor works on Alzheimer's"]
    for query in queries:
        ranks, scores, logs = hipporag.rank_docs(query, top_k=10)
