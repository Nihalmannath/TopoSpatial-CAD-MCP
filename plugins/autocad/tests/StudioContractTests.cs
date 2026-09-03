using Newtonsoft.Json;
using Xunit;

namespace TopoSpatial.AutoCAD.Tests
{
    public sealed class StudioContractTests
    {
        [Fact]
        public void ParsesTypedCamelCaseMessage()
        {
            var message = JsonConvert.DeserializeObject<StudioMessage>("{\"version\":1,\"action\":\"focus_entity\",\"expectedDrawing\":\"Plan.dwg\",\"handle\":\"1A\"}");
            Assert.Equal(1, message.Version);
            Assert.Equal("focus_entity", message.Action);
            Assert.Equal("Plan.dwg", message.ExpectedDrawing);
        }

        [Theory]
        [InlineData("focus_entity", true)]
        [InlineData("select_entity", true)]
        [InlineData("request_status", true)]
        [InlineData("zoom", false)]
        public void AcceptsOnlyKnownActions(string action, bool expected) => Assert.Equal(expected, StudioContract.IsSupportedAction(action));

        [Theory]
        [InlineData("http://127.0.0.1:8888/editor", true)]
        [InlineData("http://localhost:8888/editor", false)]
        [InlineData("https://127.0.0.1:8888/editor", false)]
        [InlineData("http://127.0.0.1:9999/editor", false)]
        public void EnforcesLoopbackEditorOrigin(string source, bool expected) => Assert.Equal(expected, StudioContract.IsTrustedSource(source));

        [Fact]
        public void RejectsMissingDrawing() => Assert.False(StudioContract.DrawingNamesMatch("", @"C:\Plans\Unit.dwg"));

        [Fact]
        public void MatchesFilenameWithoutSwitching() => Assert.True(StudioContract.DrawingNamesMatch("unit.DWG", @"C:\Plans\Unit.dwg"));

        [Fact]
        public void RejectsWrongFullPath() => Assert.False(StudioContract.DrawingNamesMatch(@"C:\Other\Unit.dwg", @"C:\Plans\Unit.dwg"));
    }
}
