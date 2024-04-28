import operator
from typing import Annotated, TypedDict

from langchain.agents import (
    AgentExecutor,
    create_tool_calling_agent,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.output_parsers.openai_tools import JsonOutputKeyToolsParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, Field

from app.core.graph.models import all_models
from app.core.graph.skills import all_skills


class Person(BaseModel):
    name: str = Field(description="The name of the person")
    role: str = Field(description="Role of the person")
    provider: str = Field(description="The provider for the llm model")
    model: str = Field(description="The llm model to use for this person")
    temperature: float = Field(description="The temperature of the llm model")


class Member(Person):
    backstory: str = Field(
        description="Description of the person's experience, motives and concerns."
    )
    tools: list[str] = Field(description="The list of tools that the person can use.")

    @property
    def persona(self) -> str:
        return f"Name: {self.name}\nRole: {self.role}\nBackstory: {self.backstory}\n"


# Create a Leader class so we can pass leader as a team member for team within team
class Leader(Person):
    pass


class Team(BaseModel):
    name: str = Field(description="The name of the team")
    members: dict[str, Member | Leader] = Field(description="The members of the team")
    provider: str = Field(description="The provider of the team leader's llm model")
    model: str = Field(description="The llm model to use for this team leader")
    temperature: float = Field(
        description="The temperature of the team leader's llm model"
    )


def update_name(name: str, new_name: str):
    """Update name at the onset."""
    if not name:
        return new_name
    return name


def update_members(
    members: dict[str, Member | Leader] | None, new_members: dict[str, Member | Leader]
):
    """Update members at the onset"""
    if not members:
        members = {}
    members.update(new_members)
    return members


class TeamState(TypedDict):
    messages: Annotated[list[BaseMessage], operator.add]
    team_name: Annotated[str, update_name]
    team_members: Annotated[dict[str, Member | Leader], update_members]
    next: str
    task: list[
        BaseMessage
    ]  # This is the current task to be perform by a team member. Its a list because Worker's MessagesPlaceholder only accepts list of messages.


class BaseNode:
    def __init__(self, provider: str, model: str, temperature: float):
        self.model = all_models[provider](model=model, temperature=temperature)
        self.final_answer_model = all_models[provider](model=model, temperature=0)

    def tag_with_name(self, ai_message: AIMessage, name: str):
        """Tag a name to the AI message"""
        ai_message.name = name
        return ai_message

    def get_team_members_name(self, team_members: dict[str, Person]):
        """Get the names of all team members as a string"""
        return ",".join(list(team_members))


class WorkerNode(BaseNode):
    worker_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "You are a team member of {team_name} and you are one of the following team members: {team_members_name}.\n"
                    "Your team members (and other teams) will collaborate with you with their own set of skills. "
                    "You are chosen by one of your team member to perform this task. Try your best to perform it using your skills. "
                    "Stay true to your perspective:\n"
                    "{persona}"
                ),
            ),
            MessagesPlaceholder(variable_name="messages"),
            MessagesPlaceholder(variable_name="task"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ]
    )

    def convert_output_to_ai_message(self, state: TeamState):
        """Convert agent executor output to ai message"""
        output = state["output"]
        return AIMessage(content=output)

    def create_agent(
        self, llm: BaseChatModel, prompt: ChatPromptTemplate, tools: list[str]
    ):
        """Create the agent executor. Tools must non-empty."""
        tools = [all_skills[tool].tool for tool in tools]
        agent = create_tool_calling_agent(llm, tools, prompt)
        executor = AgentExecutor(agent=agent, tools=tools)
        return executor

    async def work(self, state: TeamState):
        name = state["next"]
        member = state["team_members"][name]
        tools = member.tools
        team_members_name = self.get_team_members_name(state["team_members"])
        prompt = self.worker_prompt.partial(
            team_members_name=team_members_name,
            persona=member.persona,
        )
        # If member has no tools, then use a regular model instead of an agent
        if len(tools) >= 1:
            agent = self.create_agent(self.model, prompt, tools)
            chain = agent | RunnableLambda(self.convert_output_to_ai_message)
        else:
            chain = prompt.partial(agent_scratchpad=[]) | self.model
        work_chain = chain | RunnableLambda(self.tag_with_name).bind(name=member.name)
        result = await work_chain.ainvoke(state)
        return {"messages": [result]}


class LeaderNode(BaseNode):
    leader_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "You are the team leader of {team_name} and you have the following team members: {team_members_name}.\n"
                    "Your team is given a task and you have to delegate the work among your team members based on their skills.\n"
                    "Team member info:\n\n"
                    "{team_members_info}"
                ),
            ),
            MessagesPlaceholder(variable_name="messages"),
            (
                "system",
                "Given the conversation above, who should act next? Or should we FINISH? Select one of: {options}.",
            ),
        ]
    )

    def get_team_members_info(self, team_members: list[Member]):
        """Create a string containing team members name and role."""
        result = ""
        for member in team_members.values():
            result += f"name: {member.name}\nrole: {member.role}\n\n"
        return result

    def get_tool_definition(self, options: list[str]):
        """Return the tool definition to choose next team member and provide the task."""
        return {
            "type": "function",
            "function": {
                "name": "route",
                "description": "Select the next role.",
                "parameters": {
                    "title": "routeSchema",
                    "type": "object",
                    "properties": {
                        "next": {
                            "title": "Next",
                            "anyOf": [
                                {"enum": options},
                            ],
                        },
                        "task": {
                            "title": "task",
                            "description": "Provide the task to the team member.",
                        },
                    },
                    "required": ["next", "task"],
                },
            },
        }

    async def delegate(self, state: TeamState):
        team_members_name = self.get_team_members_name(state["team_members"])
        team_name = state["team_name"]
        team_members_info = self.get_team_members_info(state["team_members"])
        options = list(state["team_members"]) + ["FINISH"]
        tools = [self.get_tool_definition(options)]

        delegate_chain = (
            self.leader_prompt.partial(
                team_name=team_name,
                team_members_name=team_members_name,
                team_members_info=team_members_info,
                options=str(options),
            )
            | self.model.bind_tools(tools=tools)
            | JsonOutputKeyToolsParser(key_name="route", first_tool_only=True)
        )
        result = await delegate_chain.ainvoke(state)
        if not result:
            return {
                "task": [HumanMessage(content="No further tasks.", name=team_name)],
                "next": "FINISH",
            }
        else:
            # Convert task from string to list[HumanMessage] because Worker's MessagesPlaceholder only accepts list of messages.
            result["task"] = [
                HumanMessage(
                    content=result.get("task", "No further tasks."), name=team_name
                )
            ]
            return result


class SummariserNode(BaseNode):
    summariser_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "You are a team member of {team_name} and you have the following team members: {team_members_name}. "
                    "Your team was given a task and your team members have performed their roles and returned their responses to the team leader.\n\n"
                    "Here is the team's task:\n"
                    "'''\n"
                    "{team_task}\n"
                    "'''\n\n"
                    "These are the responses from your team members:\n"
                    "'''\n"
                    "{team_responses}\n"
                    "'''\n\n"
                    "Your role is to interpret all the responses and give the final answer to the team's task.\n"
                ),
            )
        ]
    )

    def get_team_responses(self, messages: list[BaseMessage]):
        """Create a string containing the team's responses."""
        result = ""
        for message in messages:
            result += f"{message.name}: {message.content}\n"
        return result

    async def summarise(self, state: TeamState):
        team_members_name = self.get_team_members_name(state["team_members"])
        team_name = state["team_name"]
        team_responses = self.get_team_responses(state["messages"])
        team_task = state["messages"][0].content

        summarise_chain = (
            self.summariser_prompt.partial(
                team_name=team_name,
                team_members_name=team_members_name,
                team_task=team_task,
                team_responses=team_responses,
            )
            | self.final_answer_model
            | RunnableLambda(self.tag_with_name).bind(name="FinalAnswer")
        )
        result = await summarise_chain.ainvoke(state)
        return {"messages": [result]}