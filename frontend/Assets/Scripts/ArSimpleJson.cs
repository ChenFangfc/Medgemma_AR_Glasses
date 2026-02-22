using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

public static class ArSimpleJson
{
    public static object Deserialize(string json)
    {
        if (string.IsNullOrEmpty(json))
        {
            return null;
        }

        return Parser.Parse(json);
    }

    public static string Serialize(object value)
    {
        return Serializer.Serialize(value);
    }

    private sealed class Parser : IDisposable
    {
        private const string WordBreak = "{}[],:\"";

        private readonly StringReader _reader;

        private Parser(string json)
        {
            _reader = new StringReader(json);
        }

        public static object Parse(string json)
        {
            using (Parser instance = new Parser(json))
            {
                return instance.ParseValue();
            }
        }

        public void Dispose()
        {
            _reader.Dispose();
        }

        private enum Token
        {
            None,
            CurlyOpen,
            CurlyClose,
            SquareOpen,
            SquareClose,
            Colon,
            Comma,
            String,
            Number,
            True,
            False,
            Null
        }

        private Dictionary<string, object> ParseObject()
        {
            Dictionary<string, object> table = new Dictionary<string, object>(StringComparer.Ordinal);

            _reader.Read();

            while (true)
            {
                Token token = NextToken;
                if (token == Token.None)
                {
                    return null;
                }

                if (token == Token.Comma)
                {
                    continue;
                }

                if (token == Token.CurlyClose)
                {
                    return table;
                }

                string name = ParseString();
                if (name == null)
                {
                    return null;
                }

                if (NextToken != Token.Colon)
                {
                    return null;
                }

                _reader.Read();
                table[name] = ParseValue();
            }
        }

        private List<object> ParseArray()
        {
            List<object> array = new List<object>();

            _reader.Read();

            bool parsing = true;
            while (parsing)
            {
                Token token = NextToken;
                if (token == Token.None)
                {
                    return null;
                }

                if (token == Token.Comma)
                {
                    continue;
                }

                if (token == Token.SquareClose)
                {
                    break;
                }

                array.Add(ParseValue());
            }

            return array;
        }

        private object ParseValue()
        {
            switch (NextToken)
            {
                case Token.String:
                    return ParseString();
                case Token.Number:
                    return ParseNumber();
                case Token.CurlyOpen:
                    return ParseObject();
                case Token.SquareOpen:
                    return ParseArray();
                case Token.True:
                    return true;
                case Token.False:
                    return false;
                case Token.Null:
                    return null;
                default:
                    return null;
            }
        }

        private string ParseString()
        {
            StringBuilder builder = new StringBuilder();
            _reader.Read();

            bool parsing = true;
            while (parsing)
            {
                if (_reader.Peek() == -1)
                {
                    parsing = false;
                    break;
                }

                char c = NextChar;
                if (c == '"')
                {
                    parsing = false;
                    break;
                }

                if (c == '\\')
                {
                    if (_reader.Peek() == -1)
                    {
                        parsing = false;
                        break;
                    }

                    c = NextChar;
                    switch (c)
                    {
                        case '"':
                        case '\\':
                        case '/':
                            builder.Append(c);
                            break;
                        case 'b':
                            builder.Append('\b');
                            break;
                        case 'f':
                            builder.Append('\f');
                            break;
                        case 'n':
                            builder.Append('\n');
                            break;
                        case 'r':
                            builder.Append('\r');
                            break;
                        case 't':
                            builder.Append('\t');
                            break;
                        case 'u':
                            char[] hex = new char[4];
                            for (int i = 0; i < 4; i++)
                            {
                                if (_reader.Peek() == -1)
                                {
                                    return null;
                                }

                                hex[i] = NextChar;
                            }

                            builder.Append((char)Convert.ToInt32(new string(hex), 16));
                            break;
                    }
                }
                else
                {
                    builder.Append(c);
                }
            }

            return builder.ToString();
        }

        private object ParseNumber()
        {
            string number = NextWord;
            if (number.IndexOf('.') == -1 && number.IndexOf('e') == -1 && number.IndexOf('E') == -1)
            {
                if (long.TryParse(number, NumberStyles.Integer, CultureInfo.InvariantCulture, out long parsedLong))
                {
                    return parsedLong;
                }
            }

            if (double.TryParse(number, NumberStyles.Float, CultureInfo.InvariantCulture, out double parsedDouble))
            {
                return parsedDouble;
            }

            return 0d;
        }

        private void EatWhitespace()
        {
            while (_reader.Peek() != -1)
            {
                char c = PeekChar;
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r')
                {
                    _reader.Read();
                    continue;
                }

                break;
            }
        }

        private char PeekChar
        {
            get { return Convert.ToChar(_reader.Peek()); }
        }

        private char NextChar
        {
            get { return Convert.ToChar(_reader.Read()); }
        }

        private string NextWord
        {
            get
            {
                StringBuilder word = new StringBuilder();
                while (_reader.Peek() != -1 && !IsWordBreak(PeekChar))
                {
                    word.Append(NextChar);
                }

                return word.ToString();
            }
        }

        private Token NextToken
        {
            get
            {
                EatWhitespace();

                if (_reader.Peek() == -1)
                {
                    return Token.None;
                }

                switch (PeekChar)
                {
                    case '{':
                        return Token.CurlyOpen;
                    case '}':
                        _reader.Read();
                        return Token.CurlyClose;
                    case '[':
                        return Token.SquareOpen;
                    case ']':
                        _reader.Read();
                        return Token.SquareClose;
                    case ',':
                        _reader.Read();
                        return Token.Comma;
                    case '"':
                        return Token.String;
                    case ':':
                        return Token.Colon;
                    case '-':
                    case '0':
                    case '1':
                    case '2':
                    case '3':
                    case '4':
                    case '5':
                    case '6':
                    case '7':
                    case '8':
                    case '9':
                        return Token.Number;
                }

                string word = NextWord;
                switch (word)
                {
                    case "false":
                        return Token.False;
                    case "true":
                        return Token.True;
                    case "null":
                        return Token.Null;
                }

                return Token.None;
            }
        }

        private static bool IsWordBreak(char c)
        {
            return char.IsWhiteSpace(c) || WordBreak.IndexOf(c) != -1;
        }
    }

    private sealed class Serializer
    {
        private readonly StringBuilder _builder = new StringBuilder();

        public static string Serialize(object value)
        {
            Serializer instance = new Serializer();
            instance.SerializeValue(value);
            return instance._builder.ToString();
        }

        private void SerializeValue(object value)
        {
            if (value == null)
            {
                _builder.Append("null");
                return;
            }

            if (value is string str)
            {
                SerializeString(str);
                return;
            }

            if (value is bool boolean)
            {
                _builder.Append(boolean ? "true" : "false");
                return;
            }

            if (value is IDictionary dictionary)
            {
                SerializeObject(dictionary);
                return;
            }

            if (value is IList list)
            {
                SerializeArray(list);
                return;
            }

            if (value is char charValue)
            {
                SerializeString(charValue.ToString());
                return;
            }

            if (IsNumeric(value))
            {
                SerializeNumber(value);
                return;
            }

            SerializeString(value.ToString());
        }

        private void SerializeObject(IDictionary dictionary)
        {
            bool first = true;
            _builder.Append('{');

            foreach (object key in dictionary.Keys)
            {
                if (!first)
                {
                    _builder.Append(',');
                }

                SerializeString(key.ToString());
                _builder.Append(':');
                SerializeValue(dictionary[key]);
                first = false;
            }

            _builder.Append('}');
        }

        private void SerializeArray(IList array)
        {
            _builder.Append('[');
            bool first = true;
            foreach (object value in array)
            {
                if (!first)
                {
                    _builder.Append(',');
                }

                SerializeValue(value);
                first = false;
            }

            _builder.Append(']');
        }

        private void SerializeString(string str)
        {
            _builder.Append('"');

            foreach (char c in str)
            {
                switch (c)
                {
                    case '"':
                        _builder.Append("\\\"");
                        break;
                    case '\\':
                        _builder.Append("\\\\");
                        break;
                    case '\b':
                        _builder.Append("\\b");
                        break;
                    case '\f':
                        _builder.Append("\\f");
                        break;
                    case '\n':
                        _builder.Append("\\n");
                        break;
                    case '\r':
                        _builder.Append("\\r");
                        break;
                    case '\t':
                        _builder.Append("\\t");
                        break;
                    default:
                        int codepoint = Convert.ToInt32(c);
                        if (codepoint >= 32 && codepoint <= 126)
                        {
                            _builder.Append(c);
                        }
                        else
                        {
                            _builder.Append("\\u");
                            _builder.Append(codepoint.ToString("x4"));
                        }

                        break;
                }
            }

            _builder.Append('"');
        }

        private void SerializeNumber(object number)
        {
            string numberString = Convert.ToString(number, CultureInfo.InvariantCulture);
            _builder.Append(numberString);
        }

        private static bool IsNumeric(object value)
        {
            return value is sbyte
                || value is byte
                || value is short
                || value is ushort
                || value is int
                || value is uint
                || value is long
                || value is ulong
                || value is float
                || value is double
                || value is decimal;
        }
    }
}
