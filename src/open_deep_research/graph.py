from typing import Literal

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from langgraph.constants import Send
from langgraph.graph import START, END, StateGraph
from langgraph.types import interrupt, Command

from open_deep_research.state import (
    ReportStateInput,
    ReportStateOutput,
    Sections,
    ReportState,
    SectionState,
    SectionOutputState,
    Queries,
    Feedback
)

from open_deep_research.prompts import (
    report_planner_query_writer_instructions,
    report_planner_instructions,
    query_writer_instructions, 
    section_writer_instructions,
    final_section_writer_instructions,
    section_grader_instructions,
    section_writer_inputs
)

from open_deep_research.configuration import Configuration
from open_deep_research.utils import (
    format_sections, 
    get_config_value, 
    get_search_params, 
    select_and_execute_search
)

## Nodes -- 

async def generate_report_plan(state: ReportState, config: RunnableConfig):
    """生成初始报告计划及其各个部分。

    此节点功能：
    1. 获取报告结构和搜索参数的配置信息
    2. 生成用于规划的搜索查询
    3. 使用这些查询进行网络搜索
    4. 利用大语言模型（LLM）生成包含各部分的结构化报告计划

    参数说明：
        state: 当前图状态，包含报告主题
        config: 用于模型、搜索API等的配置信息

    返回值：
        包含生成的各个部分的字典
    """

    # Inputs
    topic = state["topic"]
    feedback = state.get("feedback_on_report_plan", None)

    # Get configuration
    configurable = Configuration.from_runnable_config(config)
    report_structure = configurable.report_structure
    number_of_queries = configurable.number_of_queries
    search_api = get_config_value(configurable.search_api)
    search_api_config = configurable.search_api_config or { "max_results": 1 }  # Get the config dict, default to empty
    params_to_pass = get_search_params(search_api, search_api_config)  # Filter parameters

    # 如果 report_structure 是一个字典（dict），就将其转换为字符串。
    # 这样做通常是为了后续格式化字符串或传递给只接受字符串参数的函数，避免类型错误。
    if isinstance(report_structure, dict):
        report_structure = str(report_structure)

    # Set writer model (model used for query writing)
    writer_provider = get_config_value(configurable.writer_provider)
    writer_model_name = get_config_value(configurable.writer_model)
    writer_model_kwargs = get_config_value(configurable.writer_model_kwargs or {})
    writer_model_base_url = get_config_value(configurable.writer_model_base_url)

    writer_model = init_chat_model(model=writer_model_name, model_provider=writer_provider, model_kwargs=writer_model_kwargs,base_url=writer_model_base_url) 
    structured_llm = writer_model.with_structured_output(Queries)

    # Format system instructions
    system_instructions_query = report_planner_query_writer_instructions.format(topic=topic, report_organization=report_structure, number_of_queries=number_of_queries)

    # Generate queries  
    results = await structured_llm.ainvoke([SystemMessage(content=system_instructions_query),
                                     HumanMessage(content="生成有助于规划报告各部分的网页搜索查询。")])

    # Web search
    query_list = [query.search_query for query in results.queries]

    # Search the web with parameters
    source_str = await select_and_execute_search(search_api, query_list, params_to_pass)

    # Format system instructions
    system_instructions_sections = report_planner_instructions.format(topic=topic, report_organization=report_structure, context=source_str, feedback=feedback)

    # Set the planner
    planner_provider = get_config_value(configurable.planner_provider)
    planner_model = get_config_value(configurable.planner_model)
    planner_model_kwargs = get_config_value(configurable.planner_model_kwargs or {})
    planer_model_base_url = get_config_value(configurable.planer_model_base_url)

    # Report planner instructions（中文提示词）
    planner_message = """请生成报告的各个部分。你的回复必须包含一个 'sections' 字段，其值为各部分的列表。
每个部分必须包含以下字段: name(名称)、description(描述)、plan(规划/计划)、research(是否需要检索)、content(内容)。"""

   
    planner_llm = init_chat_model(model=planner_model, 
                                    model_provider=planner_provider,
                                    model_kwargs=planner_model_kwargs,
                                    base_url = planer_model_base_url)

    # Generate the report sections
    structured_llm = planner_llm.with_structured_output(Sections)
    report_sections = await structured_llm.ainvoke([SystemMessage(content=system_instructions_sections),
                                             HumanMessage(content=planner_message)])

    # Get sections
    sections = report_sections.sections

    return {"sections": sections}

def human_feedback(state: ReportState, config: RunnableConfig) -> Command[Literal["generate_report_plan","build_section_with_web_research"]]:
    """获取用户对报告计划的反馈并确定下一步操作。
    
    此节点功能：
    1. 格式化当前报告计划以供用户审阅
    2. 通过中断获取用户反馈
    3. 根据反馈进行路由：
       - 如果计划获得批准，则开始撰写章节
       - 如果收到反馈意见，则重新生成计划
    
    参数：
        state: 包含待审阅章节的当前图状态
        config: 工作流配置
        
    返回：
        重新生成计划或开始章节撰写的命令
    """

    # Get sections
    topic = state["topic"]
    sections = state['sections']
    sections_str = "\n\n".join(
        f"章节: {section.name}\n"
        f"描述: {section.description}\n"
        f"是否需要研究: {'是' if section.research else '否'}\n"
        for section in sections
    )

    # Get feedback on the report plan from interrupt
    interrupt_message = f"""请对以下报告计划提供反馈。
                        \n\n{sections_str}\n
                        \n这个报告计划是否符合您的需求？\n输入 'true' 表示批准报告计划。\n或者，提供反馈以重新生成报告计划："""
    
    feedback = interrupt(interrupt_message)
    feedback = feedback.get("feedback")
    # If the user approves the report plan, kick off section writing
    if  feedback == "正确":
        # Treat this as approve and kick off section writing
        return Command(goto=[
            Send("build_section_with_web_research", {"topic": topic, "section": s, "search_iterations": 0}) 
            for s in sections 
            if s.research
        ])
    
    # If the user provides feedback, regenerate the report plan 
    elif isinstance(feedback, str):
        # Treat this as feedback
        return Command(goto="generate_report_plan", 
                       update={"feedback_on_report_plan": feedback})
    else:
        raise TypeError(f"Interrupt value of type {type(feedback)} is not supported.")
    
async def generate_queries(state: SectionState, config: RunnableConfig):
    """为特定章节生成检索查询。

    此节点使用大语言模型（LLM）根据章节主题和描述生成有针对性的检索查询。

    参数：
        state: 包含章节详细信息的当前状态
        config: 配置，包括要生成的查询数量

    返回：
        包含生成的检索查询的字典
    """

    # Get state 
    topic = state["topic"]
    section = state["section"]

    # Get configuration
    configurable = Configuration.from_runnable_config(config)
    number_of_queries = configurable.number_of_queries

    # Generate queries 
    writer_provider = get_config_value(configurable.writer_provider)
    writer_model_name = get_config_value(configurable.writer_model)
    writer_model_kwargs = get_config_value(configurable.writer_model_kwargs or {})
    writer_model = init_chat_model(model=writer_model_name, model_provider=writer_provider, model_kwargs=writer_model_kwargs) 
    structured_llm = writer_model.with_structured_output(Queries)

    # Format system instructions
    system_instructions = query_writer_instructions.format(topic=topic, 
                                                           section_topic=section.description, 
                                                           number_of_queries=number_of_queries)

    # Generate queries  
    queries = await structured_llm.ainvoke([SystemMessage(content=system_instructions),
                                     HumanMessage(content="生成有助于检索信息的网页搜索查询。")])

    return {"search_queries": queries.queries}

async def search_web(state: SectionState, config: RunnableConfig):
    """为章节查询执行网页搜索。

    此节点：
    1. 获取已生成的检索查询
    2. 使用配置的搜索API执行检索
    3. 将结果格式化为可用的上下文

    参数：
        state: 包含检索查询的当前状态
        config: 搜索API的配置

    返回：
        包含搜索结果和更新后迭代次数的字典
    """

    # Get state
    search_queries = state["search_queries"]

    # Get configuration
    configurable = Configuration.from_runnable_config(config)
    search_api = get_config_value(configurable.search_api)
    search_api_config = configurable.search_api_config or {}  # Get the config dict, default to empty
    params_to_pass = get_search_params(search_api, search_api_config)  # Filter parameters

    # Web search
    query_list = [query.search_query for query in search_queries]

    # Search the web with parameters
    source_str = await select_and_execute_search(search_api, query_list, params_to_pass)

    return {"source_str": source_str, "search_iterations": state["search_iterations"] + 1}

async def write_section(state: SectionState, config: RunnableConfig) -> Command[Literal[END, "search_web"]]:
    """撰写报告的一个章节并评估是否需要进一步研究。

    此节点功能：
    1. 利用检索结果撰写章节内容
    2. 评估章节质量
    3. 根据评估结果：
       - 若质量合格，则完成该章节
       - 若质量不合格，则触发进一步研究

    参数：
        state: 包含检索结果和章节信息的当前状态
        config: 撰写和评估所需的配置信息

    返回：
        指示是完成章节还是继续研究的命令
    """

    # Get state 
    topic = state["topic"]
    section = state["section"]
    source_str = state["source_str"]

    # Get configuration
    configurable = Configuration.from_runnable_config(config)

    # Format system instructions
    section_writer_inputs_formatted = section_writer_inputs.format(topic=topic, 
                                                             section_name=section.name, 
                                                             section_topic=section.description, 
                                                             context=source_str, 
                                                             section_content=section.content)

    # Generate section  
    writer_provider = get_config_value(configurable.writer_provider)
    writer_model_name = get_config_value(configurable.writer_model)
    writer_model_kwargs = get_config_value(configurable.writer_model_kwargs or {})
    writer_model = init_chat_model(model=writer_model_name, model_provider=writer_provider, model_kwargs=writer_model_kwargs) 

    section_content = await writer_model.ainvoke([SystemMessage(content=section_writer_instructions),
                                           HumanMessage(content=section_writer_inputs_formatted)])
    
    # Write content to the section object  
    section.content = section_content.content

    # Use planner model for reflection
    planner_provider = get_config_value(configurable.planner_provider)
    planner_model = get_config_value(configurable.planner_model)
    planner_model_kwargs = get_config_value(configurable.planner_model_kwargs or {})

    # Initialize the reflection model
    reflection_model = init_chat_model(model=planner_model, 
                                           model_provider=planner_provider, model_kwargs=planner_model_kwargs).with_structured_output(Feedback)

    section_grader_instructions_formatted = section_grader_instructions.format(topic=topic, 
                                                                            section_topic=section.description,
                                                                            section=section.content, 
                                                                            number_of_follow_up_queries=configurable.number_of_queries)
    section_grader_message = ("评估报告并考虑缺失信息。 "
                              "如果评分是'通过'，则返回空字符串作为所有后续查询。 "
                              "如果评分是'失败'，则提供特定的搜索查询以收集缺失信息。")
    
    feedback = await reflection_model.ainvoke([SystemMessage(content=section_grader_instructions_formatted),
                                        HumanMessage(content=section_grader_message)])

    # If the section is passing or the max search depth is reached, publish the section to completed sections 
    if feedback.grade == "pass" or state["search_iterations"] >= configurable.max_search_depth:
        # Publish the section to completed sections 
        return  Command(
        update={"completed_sections": [section]},
        goto=END
    )

    # Update the existing section with new content and update search queries
    else:
        return  Command(
        update={"search_queries": feedback.follow_up_queries, "section": section},
        goto="search_web"
        )
    
async def write_final_sections(state: SectionState, config: RunnableConfig):
    """使用已完成的章节作为上下文，撰写无需检索的章节。

    此节点用于处理如结论或总结等章节，这些章节基于已研究的章节内容撰写，而不需要直接检索。

    参数:
        state: 当前状态，包含已完成章节作为上下文
        config: 用于撰写模型的配置信息

    返回:
        包含新撰写章节的字典
    """

    # Get configuration
    configurable = Configuration.from_runnable_config(config)

    # Get state 
    topic = state["topic"]
    section = state["section"]
    completed_report_sections = state["report_sections_from_research"]
    
    # Format system instructions
    system_instructions = final_section_writer_instructions.format(topic=topic, section_name=section.name, section_topic=section.description, context=completed_report_sections)

    # Generate section  
    writer_provider = get_config_value(configurable.writer_provider)
    writer_model_name = get_config_value(configurable.writer_model)
    writer_model_kwargs = get_config_value(configurable.writer_model_kwargs or {})
    writer_model = init_chat_model(model=writer_model_name, model_provider=writer_provider, model_kwargs=writer_model_kwargs) 
    
    section_content = await writer_model.ainvoke([SystemMessage(content=system_instructions),
                                           HumanMessage(content="根据提供的源内容生成一个报告章节。")])
    
    # Write content to section 
    section.content = section_content.content

    # Write the updated section to completed sections
    return {"completed_sections": [section]}

def gather_completed_sections(state: ReportState):
    """将已完成的章节格式化为撰写总结性章节的上下文字符串。

    此节点会收集所有已完成的研究章节，并将其格式化为一个用于撰写总结或结论性章节的上下文字符串。

    参数:
        state: 当前状态，包含已完成的章节

    返回:
        包含格式化后章节内容的字典
    """

    # List of completed sections
    completed_sections = state["completed_sections"]

    # Format completed section to str to use as context for final sections
    completed_report_sections = format_sections(completed_sections)

    return {"report_sections_from_research": completed_report_sections}

def compile_final_report(state: ReportState):
    """将所有章节汇编成最终报告。

    此节点功能：
    1. 获取所有已完成的章节
    2. 按照原始计划顺序排序
    3. 合并为最终报告

    参数:
        state: 包含所有已完成章节的当前状态

    返回:
        包含完整报告的字典
    """

    # Get sections
    sections = state["sections"]
    completed_sections = {s.name: s.content for s in state["completed_sections"]}

    # Update sections with completed content while maintaining original order
    for section in sections:
        section.content = completed_sections[section.name]

    # Compile final report
    all_sections = "\n\n".join([s.content for s in sections])

    return {"final_report": all_sections}

def initiate_final_section_writing(state: ReportState):
    """为无需检索的章节创建并行写作任务。

    此边缘函数会识别所有不需要检索的章节，
    并为每个章节创建一个并行的写作任务。

    参数:
        state: 当前状态，包含所有章节及研究上下文

    返回:
        用于并行章节写作的 Send 命令列表
    """

    # Kick off section writing in parallel via Send() API for any sections that do not require research
    return [
        Send("write_final_sections", {"topic": state["topic"], "section": s, "report_sections_from_research": state["report_sections_from_research"]}) 
        for s in state["sections"] 
        if not s.research
    ]

# Report section sub-graph -- 

# Add nodes 
section_builder = StateGraph(SectionState, output=SectionOutputState)
section_builder.add_node("generate_queries", generate_queries)
section_builder.add_node("search_web", search_web)
section_builder.add_node("write_section", write_section)

# Add edges
section_builder.add_edge(START, "generate_queries")
section_builder.add_edge("generate_queries", "search_web")
section_builder.add_edge("search_web", "write_section")

# Outer graph for initial report plan compiling results from each section -- 

# Add nodes
builder = StateGraph(ReportState, input=ReportStateInput, output=ReportStateOutput, config_schema=Configuration)
builder.add_node("generate_report_plan", generate_report_plan)
builder.add_node("human_feedback", human_feedback)
builder.add_node("build_section_with_web_research", section_builder.compile())
builder.add_node("gather_completed_sections", gather_completed_sections)
builder.add_node("write_final_sections", write_final_sections)
builder.add_node("compile_final_report", compile_final_report)

# Add edges
builder.add_edge(START, "generate_report_plan")
builder.add_edge("generate_report_plan", "human_feedback")
builder.add_edge("build_section_with_web_research", "gather_completed_sections")
builder.add_conditional_edges("gather_completed_sections", initiate_final_section_writing, ["write_final_sections"])
builder.add_edge("write_final_sections", "compile_final_report")
builder.add_edge("compile_final_report", END)


graph = builder.compile()
