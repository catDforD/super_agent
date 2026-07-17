## 各个subagent输入样例

### plc_dev 输入样例（PLC 代码开发）

自然语言

### plc_test 输入样例（PLC 代码测试）

PLC 代码，例如：

FUNCTION_BLOCK StackMin
VAR_INPUT
 	push : BOOL;
 	pop : BOOL;
 	reset : BOOL;
END_VAR
VAR_OUTPUT
 	error : BOOL;
 	status : WORD;
END_VAR
VAR_IN_OUT
 	item : INT;
 	stack : ARRAY[0..3] OF INT;
END_VAR
VAR
 	top : INT := -1;
 	i : INT;
 	minIndex : INT;
 	minValue : INT;
END_VAR

IF reset THEN
 	top := -1;
 	error := FALSE;
 	status := 16#0000;
 	RETURN;
END_IF;

error := FALSE;
status := 16#0000;

IF push THEN
        IF top >= 3 THEN
            error := TRUE;
            status := 16#8A04;
        ELSE
            top := top + 1;
            stack[top] := item;
        END_IF;
ELSIF pop AND NOT push THEN
 	IF top < 0 THEN
 	 	error := TRUE;
 	 	status := 16#8A05;
 	ELSE
 	 	// Find minimum value and its index
 	 	minIndex := 0;
 	 	minValue := stack[0];
 	 	FOR i := 1 TO top DO
 	 	 	IF stack[i] < minValue THEN
 	 	 	 	minValue := stack[i];
 	 	 	 	minIndex := i;
 	 	 	END_IF;
 	 	END_FOR;
 	 	
 	 	// Return the minimum value
 	 	item := minValue;
 	 	
 	 	// Shift elements above minIndex down by one
 	 	FOR i := minIndex TO top - 1 DO
 	 	 	stack[i] := stack[i + 1];
 	 	END_FOR;
 	 	
 	 	top := top - 1;
 	END_IF;
END_IF;
END_FUNCTION_BLOCK



### plc_repair 输入样例（PLC 代码修复）

可能也就是自然语言或者 plc 代码，这个我不太确定，需要看文档（docs/plc_subagent_api.md）给出的接口


### plc_formal 输入样例（形式化验证）


例如：

{
  "st_code": "FUNCTION_BLOCK Example\nVAR_INPUT\n    x : BOOL;\nEND_VAR\nVAR_OUTPUT\n    y : BOOL;\nEND_VAR\ny := x;\n//#ASSERT (y = x) : assert_y_equals_x\nEND_FUNCTION_BLOCK",
  "properties": [
    {
      "property_description": "输出 y 必须等于输入 x / y must equal x",
      "property": {
        "job_req": "assertion"
      }
    }
  ]
}


具体可参考文档：docs/形式化验证文档.md