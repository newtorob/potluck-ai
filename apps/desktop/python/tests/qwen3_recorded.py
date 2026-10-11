"""Exact replies of qwen3-1.7b-q4 (llama-cpp-python 0.3.23, Metal, temperature 0), recorded 2026-10-10.

Each constant is (raw text, finish_reason, completion_tokens). The prompts:
  TOOLS_*      "What's the weather in Paris right now? Use the tool." with a get_weather tool, with and without " /no_think"
  SECOND_*     the same conversation after the call: assistant tool_calls, then the tool result {"city": "Paris", "temp_c": 17, "sky": "overcast"}
  SUM_*        "What is 17 * 23? Reply with only the number." at 512 and 2048 tokens, and with " /no_think"
"""

# tools + /no_think (turn 1)
TOOLS_NO_THINK = ('<think>\n\n<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>', 'stop', 21)

# tools, thinking on (turn 1)
TOOLS_THINKING = ('<think>\nOkay, the user is asking for the current weather in Paris. I need to use the get_weather tool. The tool requires the city parameter, which is Paris. So I\'ll call the function with city set to Paris. Let me make sure there are no other parameters needed. The required field is just city, so that\'s all I need. Alright, I\'ll generate the tool call with Paris as the argument.\n</think>\n\n<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>', 'stop', 107)

# tools + /no_think, second turn with the result
SECOND_NO_THINK = ('<think>\n\n</think>\n\nThe current weather in Paris is 17°C with overcast skies.', 'stop', 19)

# tools, thinking on, second turn with the result
SECOND_THINKING = ('<think>\nOkay, the user asked for the weather in Paris. I called the get_weather function with Paris as the city. The response came back with the temperature in Celsius as 17 and the sky condition as "overcast". Now I need to present this information clearly.\n\nFirst, I\'ll mention the current temperature. Since it\'s in Celsius, I should note that. Then, the sky condition is overcast, so I\'ll describe that. Make sure the answer is concise and directly addresses the user\'s question. No need for extra details since the user only asked for the weather. Check for any errors in the data, but the response seems straightforward. Alright, time to put it all together in a friendly manner.\n</think>\n\nThe current weather in Paris is 17°C with overcast skies. Let me know if you need further details! 🌧️', 'stop', 176)

# 17 x 23, 512 tokens
SUM_512 = ("<think>\nOkay, let's see. I need to calculate 17 multiplied by 23. Hmm, how do I do that? Well, maybe I can break it down. I remember that multiplying two numbers can be done by breaking one of them into parts. Let me think... 17 times 23. Maybe I can use the distributive property here. Like, 17 times 20 plus 17 times 3. Let me check that.\n\nSo, 17 times 20 is 340, right? Because 17 times 10 is 170, times 2 is 340. Then 17 times 3 is 51. So adding those together, 340 plus 51. Let me add them. 340 plus 50 is 390, then plus 1 more is 391. Wait, is that right? Let me verify.\n\nAlternatively, maybe I can use another method. Let's see, 17 times 23. If I think of 23 as 25 minus 2, then 17 times 25 minus 17 times 2. 17 times 25. Hmm, 17 times 25. Well, 17 times 10 is 170, 17 times 20 is 340, so 17 times 25 is 340 + 170 = 410? Wait, no, that's not right. Wait, 25 is 20 + 5. So 17*20 is 340, 17*5 is 85. So 340 + 85 is 425. Then subtract 17*2 which is 34. So 425 - 34 is 391. Yeah, same answer. So 391. Let me check with another way.\n\nMaybe using the standard multiplication method. Let's write it out:\n\n   17\nx 23\n------\n   51 (17*3)\n+340 (17*20)\n------\n  391\n\nYes, that's 391. So I think that's correct. Alternatively, maybe I can use a calculator, but since", 'length', 512)

# 17 x 23, 2048 tokens
SUM_2048 = ("<think>\nOkay, let's see. I need to calculate 17 multiplied by 23. Hmm, how do I do that? Well, maybe I can break it down. I remember that multiplying two numbers can be done by breaking one of them into parts. Let me think... 17 times 23. Maybe I can use the distributive property here. Like, 17 times 20 plus 17 times 3. Let me check that.\n\nSo, 17 times 20 is 340, right? Because 17 times 10 is 170, times 2 is 340. Then 17 times 3 is 51. So adding those together, 340 plus 51. Let me add them. 340 plus 50 is 390, then plus 1 more is 391. Wait, is that right? Let me verify.\n\nAlternatively, maybe I can use another method. Let's see, 17 times 23. If I think of 23 as 25 minus 2, then 17 times 25 minus 17 times 2. 17 times 25. Hmm, 17 times 25. Well, 17 times 10 is 170, 17 times 20 is 340, so 17 times 25 is 340 + 170 = 410? Wait, no, that's not right. Wait, 25 is 20 + 5. So 17*20 is 340, 17*5 is 85. So 340 + 85 is 425. Then subtract 17*2 which is 34. So 425 - 34 is 391. Yeah, same answer. So 391. Let me check with another way.\n\nMaybe using the standard multiplication method. Let's write it out:\n\n   17\nx 23\n------\n   51 (17*3)\n+340 (17*20)\n------\n  391\n\nYes, that's 391. So I think that's correct. Alternatively, maybe I can use a calculator, but since I'm doing it manually, I need to make sure. Wait, 17 times 23. Let me think of 17*20=340, 17*3=51, total 340+51=391. Yep, that's the same. So I think the answer is 391.\n</think>\n\n391", 'stop', 594)

# 17 x 23 /no_think, 200 tokens
SUM_NO_THINK = ('<think>\n\n</think>\n\n391', 'stop', 7)
