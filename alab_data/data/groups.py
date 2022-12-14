from typing import List, Union
import networkx as nx
from bson import ObjectId
import matplotlib.pyplot as plt
import itertools as itt
from networkx.drawing.nx_agraph import graphviz_layout
from .nodes import (
    Material,
    Action,
    Measurement,
    Analysis,
    WholeIngredient,
)
import warnings

ALLOWED_NODE_TYPE = Union[Material, Action, Measurement, Analysis]


class Sample:
    def __init__(
        self,
        name: str,
        description: str = "",
        nodes: List[ALLOWED_NODE_TYPE] = None,
        tags: List[str] = None,
        **parameters,
    ):
        self.name = name
        self.description = description
        self.graph = nx.DiGraph()
        self._id = ObjectId()
        self.parameters = parameters

        if tags is None:
            self.tags = []
        else:
            self.tags = tags
        self.nodes = []
        if nodes is not None:
            for node in nodes:
                # do it this way to type check each node before adding it to this Sample
                self.add_node(node)

    @property
    def id(self):
        return self._id

    def add_node(self, node: ALLOWED_NODE_TYPE):
        if not any(
            [isinstance(node, x) for x in [Material, Action, Analysis, Measurement]]
        ):
            raise ValueError(
                "Node must be a Material, Action, Analysis, or Measurement object!"
            )
        self.graph.add_node(node.id, type=node.__class__.__name__, name=node.name)
        for upstream in node.upstream:
            if upstream["node_id"] not in self.graph.nodes:
                self.graph.add_node(
                    upstream["node_id"], type=upstream["node_type"], name=""
                )  # TODO how should we name nodes that are outside of the sample scope? Currently just empty name
            self.graph.add_edge(upstream["node_id"], node.id)
        for downstream in node.downstream:
            if downstream["node_id"] not in self.graph.nodes:
                self.graph.add_node(
                    downstream["node_id"], type=downstream["node_type"], name=""
                )  # TODO how should we name nodes that are outside of the sample scope? Currently just empty name
            self.graph.add_edge(node.id, downstream["node_id"])
        self.nodes.append(node)

    def add_linear_process(self, actions: List[Action]):
        """
        Add a linear process to the sample. A "linear process" is a series of Actions where the total output Material from each Action is fed into the follwing Action.

        Essentially, this is a helper function to stitch basic processes into a valid graph
        """
        ## Make sure a linear process is appropriate for the given sequence of Actions
        for a in actions[:-1]:
            if len(a.generated_materials) not in [0, 1]:
                raise ValueError(
                    "All Actions of a linear process (except the final Action) must generate exactly one material!"
                )

        for action0, action1 in zip(actions, actions[1:]):
            if len(action1.ingredients) == 0:
                continue  # ingredients will be automatically set to the output material of action0
            if not any(
                [
                    ingredient.material in action0.generated_materials
                    for ingredient in action1.ingredients
                ]
            ):
                raise ValueError(
                    f"No ingredients for {action1} were generated by {action0}. In a linear process, each Action must use Material(s) generated by the preceding Action. If ingredients are already specified for an Action, at least one must be generated by the preceding Action."
                )

        # add the initial action + ingredient materials
        for ingredient in actions[0].ingredients:
            self.add_node(ingredient.material)
        self.add_node(actions[0])

        # add the intervening actions and implicit intermediate materials
        for action0, action1 in zip(actions, actions[1:]):
            if len(action0.generated_materials) == 0:
                intermediate_material = action0.make_generic_generated_material()
            elif len(action0.generated_materials) == 1:
                intermediate_material = action0.generated_materials[0]
            self.add_node(intermediate_material)
            if len(action1.ingredients) == 0:
                action1.add_ingredient(WholeIngredient(intermediate_material))
            # action1.add_ingredient(WholeIngredient(material=intermediate_material))
            self.add_node(action1)

        # add the final material
        if len(actions[-1].generated_materials) == 0:
            actions[-1].make_generic_generated_material()
        for final_material in actions[-1].generated_materials:
            self.add_node(final_material)

    def has_valid_graph(self) -> bool:
        is_acyclic = nx.is_directed_acyclic_graph(self.graph)
        num_connected_components = len(
            list(nx.connected_components(self.graph.to_undirected()))
        )
        return is_acyclic and (num_connected_components == 1)

    def get_action_graph(self, include_outside_nodes: bool = False) -> nx.DiGraph:
        """
        Return a reduced version of the Sample graph that only contains Actions. useful for comparing the process involved in making two Samples

        Args:
            include_outside_nodes (bool, optional): If True, include nodes that are not part of the Sample (ie Actions that are immediately upstream of the first Action within this Sample) in the returned graph. Defaults to False.
        """
        g = self.graph.copy()

        nodes_to_delete = [
            nid for nid, ndata in g.nodes(data=True) if ndata["type"] != "Action"
        ]

        for node in nodes_to_delete:
            g.add_edges_from(itt.product(g.predecessors(node), g.successors(node)))
            g.remove_node(node)

        if not include_outside_nodes:
            nodes_to_delete = []
            for nid in g.nodes:
                if not any([nid == node.id for node in self.nodes]):
                    nodes_to_delete.append(nid)
            for nid in nodes_to_delete:
                g.remove_node(nid)
        return g

    def to_dict(self) -> dict:
        node_dict = {
            nodetype.__name__: []
            for nodetype in [Material, Action, Analysis, Measurement]
        }
        for node in self.nodes:
            node_dict[type(node).__name__].append(node.id)

        entry = {
            "_id": self.id,
            "name": self.name,
            "description": self.description,
            "nodes": node_dict,
            "tags": self.tags,
        }

        for parameter_name in self.parameters:
            if parameter_name in entry:
                raise ValueError(
                    f"Parameter name {parameter_name} is not allowed, as it collides with a default key in a Sample entry! Please change this name and try again."
                )
        entry.update(self.parameters)
        entry.pop("version_history", None)

        return entry

    def plot(self, with_labels: bool = True, ax: plt.Axes = None):
        """Plots the sample graph. This is pretty chaotic with branched graphs, but can give a qualitative sense of the experimental procedure

        Args:
            with_labels (bool, optional): Whether to show the node names. Defaults to True.
            ax (matplotlib.pyplot.Axes, optional): Existing plot Axes to draw the graph onto. If None, a new plot figure+axes will be created. Defaults to None.
        """
        if ax is None:
            fig, ax = plt.subplots()

        color_key = {
            nodetype: plt.cm.tab10(i)
            for i, nodetype in enumerate(
                ["Material", "Action", "Analysis", "Measurement"]
            )
        }
        node_colors = []
        node_labels = {}
        for node in self.graph.nodes:
            node_labels[node] = self.graph.nodes[node]["name"]
            node_colors.append(color_key[self.graph.nodes[node]["type"]])
        try:
            layout = graphviz_layout(self.graph, prog="dot")
        except:
            warnings.warn(
                "Could not use graphviz layout, falling back to default networkx layout. Ensure that graphviz and pygraphviz are installed to enable hierarchical graph layouts. This only affects graph visualization."
            )
            layout = nx.spring_layout(self.graph)
        nx.draw(
            self.graph,
            with_labels=with_labels,
            node_color=node_colors,
            labels=node_labels,
            pos=layout,
            ax=ax,
        )

    def _sort_nodes(self):
        """
        sort the node list in graph hierarchical order
        """
        weights = list(nx.topological_sort(self.graph))
        self.nodes.sort(key=lambda node: weights.index(node.id))

    def __repr__(self):
        return f"<Sample: {self.name}>"

    def __eq__(self, other):
        if not isinstance(other, Sample):
            return False
        return other.id == self.id


def action_sequence_distance(
    s1: Sample, s2: Sample, include_outside_nodes: float = False
) -> float:
    """
    Compute the distance between action sequences of two samples. This is the graph edit distance between subgraphs of each sample, where each subgraph is reduced to contain only Actions. Actions are considered equivalent if they have the same name.

    Args:
        s1 (Sample): Sample 1
        s2 (Sample): Sample 2
        include_outside_nodes (bool, optional): If True, include nodes that are not part of the Sample (ie Actions that are immediately upstream of the first Action within this Sample) in the returned graph. Defaults to False.

    Returns:
        float: distance between the two samples Action sequences. 0 means identical.
    """

    def node_match(n1, n2):
        return (n1["type"] == n2["type"]) and (n1["name"] == n2["name"])

    g1 = s1.get_action_graph(include_outside_nodes=include_outside_nodes)
    g2 = s2.get_action_graph(include_outside_nodes=include_outside_nodes)
    return nx.graph_edit_distance(g1, g2, node_match=node_match)
