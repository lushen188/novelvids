"""Story and cover instructions share the Chinese skill pack."""

from prompts.skill_pack import task_facts

# 封面直接渲染给生图模型，不得携带五格设定卡/三视图版式规则（image skill
# 全文含角色参考图版式与资产卡协议，会把封面带偏成设定卡拼版），只保留
# 封面自身的版式指令，与备份版（1.20.0 二开）已验证的封面行为对齐。
COVER_INSTRUCTIONS = {
    "zh": """\
项目封面任务：生成单幅完整电影主视觉（key art）。
严格按任务输入 aspect_ratio 的画幅构图，突出主要冲突与氛围，背景必须是有纵深的剧情场景。
画面主体即使是角色，也按单人电影海报的单幅完整构图呈现；禁止五格角色设定卡、角色三视图、多格拼版、脸部特写格和纯色浅灰设定背景。
无标题、无文字、无 Logo、无水印、无边框；画风按剧情题材与氛围完整表现。
将画风与构图写进完整提示词，不依赖生成工具再次补充。""",
    "en": """\
Project cover task: produce one single full cinematic key art frame.
Follow the aspect_ratio from the task input strictly; highlight the core conflict and mood, with a background that is a plot scene of real depth.
Even when the subject is a character, present it as a single complete movie-poster composition; never use character design cards, multi-view turnarounds, multi-panel grids, face-crop panels, or flat gray sheet backgrounds.
No title, no text, no logo, no watermark, no border; render the art style fully according to the story genre and mood.
Write the final style and composition into the complete prompt instead of relying on the generation tool to fill anything in.""",
}


def render_cover_prompt(*, name, book_types, story_outline, prompt_language="en", aspect_ratio="2:3"):
    return COVER_INSTRUCTIONS["en" if prompt_language == "en" else "zh"] + "\n" + task_facts({
        "purpose": "project_cover", "name": name, "genres": book_types,
        "outline": story_outline, "aspect_ratio": aspect_ratio,
    })
