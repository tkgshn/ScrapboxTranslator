import json
import asyncio
import aiohttp
import openai
import re
import tiktoken
from asyncio import Semaphore

MAX_TOKENS = 1300 # 4096 (gpt3.5 max token) - 500 (prompt) - 2000 (output)
SEMAPHORE_LIMIT = 50 # not sure if this is the best limit, but at least it works

INPUT_PATH = "input_json/blu3mo_filtered.json"
OUTPUT_PATH = "output_json/blu3mo_filtered.json"

PROMPT = """
You are a language translator.
Target Language: English

# Task
Translate texts from the source language to English, and output the translated texts.

# Rules
- Always preserve \\n and \\s.
- Keep brackets unchanged: Brackets of [text] and [text.icon] must be kept. The content inside square brackets must never be changed.
- Preserve markup symbols like >, `, [].
"""

FEW_SHOT_USER = ["[りんご]\\n\\s\\sバナナ\\n\\s\\s\\s[ダイアモンド.icon]\\n"]

FEW_SHOT_ASSISTANT = ["[apple]\\n\\s\\sbanana\\n\\s\\s\\s[diamond.icon]\\n"]

def num_tokens_from_string(string: str, encoding_name: str = "p50k_base") -> int:
    """Returns the number of tokens in a text string."""
    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens

async def async_translate(session, text, sem):
    async with sem:
        # Replace leading spaces/tabs/full width spaces with \s
        text = re.sub(r'^([ \t　]+)', lambda m: '\\s' * len(m.group(1)), text, flags=re.MULTILINE)

        # Replace newlines with \n
        text = text.replace('\n', '\\n')

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {openai.api_key}"
        }

        data = {
            "model": "gpt-3.5-turbo",
            "messages": [
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": FEW_SHOT_USER[0]},
                {"role": "assistant", "content": FEW_SHOT_ASSISTANT[0]},
                {"role": "user", "content": text}
            ],
            "temperature": 0,
            "max_tokens": 2000,
        }

        RETRY_LIMIT = 3
        translated_text = text

        text_line_count = len(text.split("\\n"))

        for _ in range(RETRY_LIMIT):
            try:
                async with session.post("https://api.openai.com/v1/chat/completions", headers=headers, json=data) as resp:
                    response = await resp.json()
                    translated_text = response["choices"][0]["message"]["content"]
                    translated_line_count = len(translated_text.split("\\n"))
                    # Allowing ±3 line error, because it happens a lot
                    if translated_line_count < (text_line_count - 3) or (text_line_count + 3) < translated_line_count:
                        raise Exception("Wrong line count of translated text")
                    # Replace \n back to newline
                    translated_text = translated_text.replace('\\n', '\n')
                    # Replace \s back to spaces
                    translated_text = re.sub(r'\\s', ' ', translated_text)
            except Exception as e:
                print(f"Error occurred while making request: {e}")
                print(f"Data: {data}")
                print("Trying again until limit")
                data["temperature"] = 1 # To get different result
                continue
            break

        return translated_text

async def translate_titles(session, title_list, sem):
    translated_titles = []
    title_chunks = []
    title_chunk = ""

    for title in title_list:
        tokens_count = num_tokens_from_string(title_chunk + title + "\n")
        if tokens_count < MAX_TOKENS:
            title_chunk += title + "\n"
        else:
            title_chunk = title_chunk[:-1]
            title_chunks.append(title_chunk)
            title_chunk = title + "\n"

    if title_chunk:
        title_chunks.append(title_chunk)

    # Run async_translate concurrently for all title chunks
    for chunk in title_chunks:
        print(len(chunk.split("\n")))

    async def translate_and_fix(session, chunk, sem):
        titles = chunk.split("\n")
        print("Translating titles batch: '" + titles[0] + "...'")
        translated_chunk = (await async_translate(session, chunk, sem)).split("\n")
        print("Lengths Compare: ", len(translated_chunk), len(titles))
        # Adjusting title list length so that it could be zipped correctly.
        if len(translated_chunk) > len(titles):
            translated_chunk_titles = translated_chunk[:len(titles)]
        if len(translated_chunk) < len(titles):
            filling_titles = titles[len(translated_chunk):len(titles)]
            translated_chunk.extend(filling_titles)
        return translated_chunk

    translation_tasks = [translate_and_fix(session, chunk, sem) for chunk in title_chunks]
    translated_chunks = await asyncio.gather(*translation_tasks)

    # Combine the translated chunks into a single list
    for chunk in translated_chunks:
        translated_titles.extend(chunk)
        # print("Count: ", len(chunk), len(translated_titles), len(title_list))

    print("Title Translation Done")
    return translated_titles

async def translate_page(session, page_text, sem):
    print("Translating page batch: '" + page_text[:20] + "...'")
    token_count = num_tokens_from_string(page_text)
    if token_count <= MAX_TOKENS:
        return await async_translate(session, page_text, sem)
    else:
        lines = page_text.split("\n")
        current_token_count = 0
        split_index = 0

        for idx, line in enumerate(lines):
            line_token_count = num_tokens_from_string(line)
            if current_token_count + line_token_count >= MAX_TOKENS:
                split_index = idx
                break
            else:
                current_token_count += line_token_count

        if split_index == 0:
            print(f"Warning: A single line in the text has more tokens ({token_count}) than the maximum allowed ({MAX_TOKENS}). Skipping translation for this line.")
            translated = await translate_page(session, "\n".join(lines[1:]), sem)
            return translated
        else:
            first_half = "\n".join(lines[:split_index])
            second_half = "\n".join(lines[split_index:])
            first_half_translated = await async_translate(session, first_half, sem)
            second_half_translated = await translate_page(session, second_half, sem)
            return first_half_translated + "\n" + second_half_translated


async def translate_json_file(input_file, output_file, sem):
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    title_translation_dict = {}

    # Translate page titles
    title_list = [page['title'] for page in data['pages']]

    async with aiohttp.ClientSession() as session:
        translated_titles = await translate_titles(session, title_list, sem)

        for original_title, translated_title in zip(title_list, translated_titles):
            title_translation_dict[original_title] = translated_title

        for page, translated_title in zip(data['pages'], translated_titles):
            page['title'] = translated_title

        # Translate lines with translated titles replaced
        translation_tasks = []

        for page in data['pages']:
            page_text = "\n".join(page['lines'])

            for jp_title, en_title in title_translation_dict.items():
                page_text = page_text.replace(f"[{jp_title}]", f"[{en_title}]")
                page_text = page_text.replace(f"[{jp_title}.icon]", f"[{en_title}.icon]")
                page_text = page_text.replace(f"#{jp_title}", f"#{en_title}")

            translation_tasks.append(translate_page(session, page_text, sem))

        translated_texts = await asyncio.gather(*translation_tasks)

        for page, translated_text in zip(data['pages'], translated_texts):
            page['lines'] = translated_text.split("\n")

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

async def main():
    sem = Semaphore(SEMAPHORE_LIMIT)
    await translate_json_file(INPUT_PATH, OUTPUT_PATH, sem)

asyncio.run(main())
